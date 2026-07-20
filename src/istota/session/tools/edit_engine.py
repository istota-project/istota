"""Fuzzy, multi-edit matching engine for the native brain's ``Edit`` tool.

Model-free pure logic ported from pi's ``edit-diff.ts``
(``~/Repos/pi/packages/coding-agent/src/core/tools/edit-diff.ts``). The tool
layer (``files.make_edit_tool``) handles path resolution / confinement / I/O;
this module only decides *what bytes to write* given the current file content
and a list of edits.

Matching is exact-first, then a bounded fuzzy fallback: exact ``str.find`` is
tried before any normalization, and the fuzzy pass only tolerates
trailing-whitespace drift and a fixed set of quote/dash/space substitutions
(Unicode NFKC, smart quotes → ASCII, Unicode dashes → ``-``, exotic spaces →
regular space). It deliberately does **not** tolerate leading-indentation or
internal-whitespace reflow, so a fuzzy match can't silently land on the wrong
region.

When any edit in a batch needs the fuzzy path, the whole batch matches against
fuzzy-normalized content but the write is produced by
``apply_replacements_preserving_unchanged_lines`` — only the lines an edit
actually touches are rewritten; every other line is copied byte-for-byte from
the original, so fuzzy matching never reflows quotes/whitespace outside the
edited lines.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Smart single quotes: ' ' ‚ ‛  →  '
_SMART_SINGLE = re.compile("[‘’‚‛]")
# Smart double quotes: " " „ ‟  →  "
_SMART_DOUBLE = re.compile("[“”„‟]")
# Dashes/hyphens: U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash,
# U+2013 en-dash, U+2014 em-dash, U+2015 horizontal bar, U+2212 minus  →  -
_DASHES = re.compile("[‐‑‒–—―−]")
# Special spaces: NBSP, U+2002–U+200A, narrow NBSP, medium math space,
# ideographic space  →  regular space
_SPACES = re.compile("[  -   　]")

# Split content into lines that retain their trailing newline (the last line
# keeps no newline). Mirrors the TS ``/[^\n]*\n|[^\n]+/g`` match.
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")


class EditError(Exception):
    """A batch of edits could not be applied. Carries a model-facing message."""


@dataclass(frozen=True)
class Edit:
    old_string: str
    new_string: str


@dataclass(frozen=True)
class Replacement:
    match_index: int
    match_length: int
    new_text: str


@dataclass(frozen=True)
class _MatchedEdit:
    edit_index: int
    match_index: int
    match_length: int
    new_text: str


@dataclass(frozen=True)
class FuzzyMatch:
    found: bool
    index: int
    match_length: int
    used_fuzzy: bool
    # When exact: the original content. When fuzzy: the normalized content —
    # offsets returned are in *that* content's space.
    content_for_replacement: str


@dataclass(frozen=True)
class AppliedEdits:
    base_content: str
    new_content: str


# --------------------------------------------------------------------------- #
# Line endings & BOM
# --------------------------------------------------------------------------- #


def detect_line_ending(content: str) -> str:
    """The file's dominant line ending: ``"\\r\\n"`` or ``"\\n"``."""
    crlf = content.find("\r\n")
    lf = content.find("\n")
    if lf == -1:
        return "\n"
    if crlf == -1:
        return "\n"
    return "\r\n" if crlf < lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def strip_bom(content: str) -> tuple[str, str]:
    """Return ``(bom, text)`` — the leading UTF-8 BOM (if any) split off."""
    if content.startswith("﻿"):
        return "﻿", content[1:]
    return "", content


# --------------------------------------------------------------------------- #
# Fuzzy normalization & matching
# --------------------------------------------------------------------------- #


def normalize_for_fuzzy_match(text: str) -> str:
    """Normalize for fuzzy matching: NFKC, strip trailing whitespace per line,
    smart quotes → ASCII, Unicode dashes → ``-``, exotic spaces → regular space."""
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _SMART_SINGLE.sub("'", text)
    text = _SMART_DOUBLE.sub('"', text)
    text = _DASHES.sub("-", text)
    text = _SPACES.sub(" ", text)
    return text


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatch:
    """Find ``old_text`` in ``content`` — exact match first, then fuzzy."""
    exact = content.find(old_text)
    if exact != -1:
        return FuzzyMatch(
            found=True,
            index=exact,
            match_length=len(old_text),
            used_fuzzy=False,
            content_for_replacement=content,
        )

    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    idx = fuzzy_content.find(fuzzy_old)
    if idx == -1:
        return FuzzyMatch(
            found=False,
            index=-1,
            match_length=0,
            used_fuzzy=False,
            content_for_replacement=content,
        )
    return FuzzyMatch(
        found=True,
        index=idx,
        match_length=len(fuzzy_old),
        used_fuzzy=True,
        content_for_replacement=fuzzy_content,
    )


def _count_occurrences(content: str, old_text: str) -> int:
    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    return fuzzy_content.count(fuzzy_old)


# --------------------------------------------------------------------------- #
# Replacement application
# --------------------------------------------------------------------------- #


def _split_lines_with_endings(content: str) -> list[str]:
    return _LINE_RE.findall(content)


@dataclass(frozen=True)
class _LineSpan:
    start: int
    end: int


def _get_line_spans(content: str) -> list[_LineSpan]:
    spans: list[_LineSpan] = []
    offset = 0
    for line in _split_lines_with_endings(content):
        spans.append(_LineSpan(start=offset, end=offset + len(line)))
        offset += len(line)
    return spans


def _apply_replacements(content: str, replacements: list[Replacement], offset: int = 0) -> str:
    """Splice replacements into ``content`` in reverse order so offsets stay
    stable. ``offset`` shifts each replacement's absolute index into the slice's
    local coordinate space."""
    result = content
    for r in reversed(replacements):
        idx = r.match_index - offset
        result = result[:idx] + r.new_text + result[idx + r.match_length :]
    return result


def _get_replacement_line_range(lines: list[_LineSpan], r: Replacement) -> tuple[int, int]:
    start = r.match_index
    end = r.match_index + r.match_length

    start_line = -1
    for i, line in enumerate(lines):
        if line.start <= start < line.end:
            start_line = i
            break
    if start_line == -1:
        raise EditError("Replacement range is outside the base content.")

    end_line = start_line
    while end_line < len(lines) and lines[end_line].end < end:
        end_line += 1
    if end_line >= len(lines):
        raise EditError("Replacement range is outside the base content.")

    return start_line, end_line + 1


def apply_replacements_preserving_unchanged_lines(
    original: str, base: str, replacements: list[Replacement]
) -> str:
    """Apply replacements matched against ``base`` (a normalized view) to
    ``original`` while preserving every unchanged line byte-for-byte.

    Each replacement is widened to the lines it touches; those lines are
    rewritten from ``base``, all others copied from ``original``. ``base`` and
    ``original`` must have identical line counts."""
    original_lines = _split_lines_with_endings(original)
    base_lines = _get_line_spans(base)
    if len(original_lines) != len(base_lines):
        raise EditError(
            "Cannot preserve unchanged lines because the base content has a "
            "different line count."
        )

    groups: list[dict] = []
    for r in sorted(replacements, key=lambda x: x.match_index):
        start_line, end_line = _get_replacement_line_range(base_lines, r)
        if groups and start_line < groups[-1]["end_line"]:
            groups[-1]["end_line"] = max(groups[-1]["end_line"], end_line)
            groups[-1]["replacements"].append(r)
            continue
        groups.append({"start_line": start_line, "end_line": end_line, "replacements": [r]})

    original_line_index = 0
    result_parts: list[str] = []
    for group in groups:
        result_parts.append("".join(original_lines[original_line_index : group["start_line"]]))
        group_start = base_lines[group["start_line"]].start
        group_end = base_lines[group["end_line"] - 1].end
        result_parts.append(
            _apply_replacements(base[group_start:group_end], group["replacements"], group_start)
        )
        original_line_index = group["end_line"]
    result_parts.append("".join(original_lines[original_line_index:]))
    return "".join(result_parts)


# --------------------------------------------------------------------------- #
# Failure messages
# --------------------------------------------------------------------------- #


def _not_found_error(path: str, i: int, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"Could not find the exact text in {path}. The text must match exactly, "
            "including all whitespace and newlines."
        )
    return EditError(
        f"Could not find edits[{i}] in {path}. The text must match exactly, "
        "including all whitespace and newlines."
    )


def _duplicate_error(path: str, i: int, total: int, occurrences: int) -> EditError:
    if total == 1:
        return EditError(
            f"Found {occurrences} occurrences of the text in {path}. It must be "
            "unique — include more surrounding context."
        )
    return EditError(
        f"Found {occurrences} occurrences of edits[{i}] in {path}. It must be "
        "unique — include more surrounding context."
    )


def _empty_old_error(path: str, i: int, total: int) -> EditError:
    if total == 1:
        return EditError(f"old_string must not be empty in {path}.")
    return EditError(f"edits[{i}].old_string must not be empty in {path}.")


def _no_change_error(path: str, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"No changes made to {path}; the replacement produced identical content."
        )
    return EditError(f"No changes made to {path}; the replacements produced identical content.")


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #


def apply_edits_to_normalized_content(
    normalized_content: str, edits: list[Edit], path: str
) -> AppliedEdits:
    """Apply one or more exact-or-fuzzy edits to LF-normalized content.

    All edits match against the same original content; replacements apply in
    reverse offset order so offsets stay stable. If any edit needs fuzzy
    matching, the batch runs in fuzzy-normalized space and the line-level
    changes are overlaid onto the original so untouched lines keep their bytes.
    Raises ``EditError`` (with a model-facing message) on any failure."""
    normalized_edits = [
        Edit(old_string=normalize_to_lf(e.old_string), new_string=normalize_to_lf(e.new_string))
        for e in edits
    ]
    total = len(normalized_edits)

    for i, e in enumerate(normalized_edits):
        if len(e.old_string) == 0:
            raise _empty_old_error(path, i, total)

    initial = [fuzzy_find_text(normalized_content, e.old_string) for e in normalized_edits]
    used_fuzzy = any(m.used_fuzzy for m in initial)
    base = normalize_for_fuzzy_match(normalized_content) if used_fuzzy else normalized_content

    matched: list[_MatchedEdit] = []
    for i, e in enumerate(normalized_edits):
        m = fuzzy_find_text(base, e.old_string)
        if not m.found:
            raise _not_found_error(path, i, total)
        occurrences = _count_occurrences(base, e.old_string)
        if occurrences > 1:
            raise _duplicate_error(path, i, total, occurrences)
        matched.append(
            _MatchedEdit(
                edit_index=i,
                match_index=m.index,
                match_length=m.match_length,
                new_text=e.new_string,
            )
        )

    matched.sort(key=lambda x: x.match_index)
    for prev, cur in zip(matched, matched[1:]):
        if prev.match_index + prev.match_length > cur.match_index:
            raise EditError(
                f"edits[{prev.edit_index}] and edits[{cur.edit_index}] overlap in "
                f"{path}. Merge them into one edit or target disjoint regions."
            )

    replacements = [
        Replacement(match_index=m.match_index, match_length=m.match_length, new_text=m.new_text)
        for m in matched
    ]
    if used_fuzzy:
        new_content = apply_replacements_preserving_unchanged_lines(
            normalized_content, base, replacements
        )
    else:
        new_content = _apply_replacements(base, replacements)

    if normalized_content == new_content:
        raise _no_change_error(path, total)

    return AppliedEdits(base_content=normalized_content, new_content=new_content)
