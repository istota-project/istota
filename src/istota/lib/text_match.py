"""Literal search terms, SQLite predicates and plain-text snippet offsets."""

import re
import unicodedata
from dataclasses import dataclass

MARK_OPEN = "\ue000"
MARK_CLOSE = "\ue001"
_FTS_PUNCTUATION = frozenset('*^:()-+"')
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class Term:
    text: str
    phrase: bool = False


def parse_query(q: str, *, phrases: bool = True, max_terms: int = 8) -> list[Term]:
    query = unicodedata.normalize("NFC", q[:200])
    pattern = r'"([^"]*)"|(\S+)' if phrases else r'(\S+)'
    terms = []
    for match in re.finditer(pattern, query):
        if len(terms) >= max_terms:
            break
        phrase = phrases and match.group(1) is not None
        value = (match.group(1) if phrase or not phrases else match.group(2)).strip()
        if value and not all(c in _FTS_PUNCTUATION or c.isspace() for c in value):
            terms.append(Term(value, phrase))
    return terms


def _check_mode(mode: str) -> None:
    if mode not in ("strict", "relaxed"):
        raise ValueError(f"unknown match mode: {mode!r}")


def fts5_match(terms: list[Term], mode: str) -> str:
    _check_mode(mode)
    quoted = []
    for term in terms:
        value = term.text.replace('"', '""')
        quoted.append('"' + value + '"' + ("" if term.phrase else "*"))
    return (" " if mode == "strict" else " OR ").join(quoted) or '""'


def like_predicate(columns: list[str], terms: list[Term], mode: str) -> tuple[str, list[str]]:
    _check_mode(mode)
    for column in columns:
        if not re.fullmatch(r"[a-z_][a-z0-9_.]*", column):
            raise ValueError(f"invalid search column: {column!r}")
    if not terms or not columns:
        return "0", []
    predicates = []
    params = []
    for term in terms:
        value = term.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicates.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns) + ")")
        params.extend([f"%{value}%"] * len(columns))
    return (" AND " if mode == "strict" else " OR ").join(predicates), params


def text_matches(text: str | None, terms: list[Term], mode: str) -> bool:
    _check_mode(mode)
    if not text or not terms:
        return False
    value = text.translate(_ASCII_LOWER)
    matches = [term.text.translate(_ASCII_LOWER) in value for term in terms]
    return all(matches) if mode == "strict" else any(matches)


def terms_to_plain(terms: list[Term]) -> str:
    return " ".join(term.text for term in terms)


def _offsets(marked: list[bool]) -> list[list[int]]:
    spans = []
    start = None
    for i, active in enumerate([*marked, False]):
        if active and start is None:
            start = i
        elif not active and start is not None:
            spans.append([start, i])
            start = None
    return spans


def markers_to_offsets(s: object) -> tuple[str, list[list[int]]]:
    raw = str(s) if s else ""
    chars = []
    spans = []
    start = None
    for char in raw:
        if char == MARK_OPEN:
            start = len(chars)
        elif char == MARK_CLOSE:
            if start is not None:
                spans.append((start, len(chars)))
                start = None
        else:
            chars.append(char)
    marked = [False] * len(chars)
    for start, end in spans:
        marked[start:end] = [True] * (end - start)
    normalized = []
    flags = []
    # Map the offsets while collapsing whitespace, so line breaks never shift marks.
    for match in re.finditer(r"\s+|\S+", "".join(chars)):
        value = match.group()
        if value.isspace():
            if normalized and match.end() < len(chars):
                normalized.append(" ")
                flags.append(any(marked[match.start():match.end()]))
        else:
            normalized.extend(value)
            flags.extend(marked[match.start():match.end()])
    return "".join(normalized), _offsets(flags)


def make_snippet(text: object, terms: list[Term], width: int = 160) -> tuple[str, list[list[int]]]:
    raw = str(text) if text else ""
    value = " ".join(raw.replace(MARK_OPEN, "").replace(MARK_CLOSE, "").split())
    if not value or width <= 0:
        return "", []
    matches = []
    for term in terms:
        needle = " ".join(term.text.split())
        if needle:
            matches.extend((m.start(), m.end()) for m in re.finditer(re.escape(needle), value, re.IGNORECASE))
    first = min((start for start, _ in matches), default=0)
    lo = max(0, first - width // 2)
    if lo:
        while lo < first and value[lo - 1] != " ":
            lo += 1
    if lo == first:
        while lo > 0 and value[lo - 1] != " ":
            lo -= 1
    hi = min(len(value), lo + width)
    if hi < len(value):
        while hi > first and value[hi] != " ":
            hi -= 1
    first_end = max((end for start, end in matches if start == first), default=first + 1)
    if hi < first_end:
        hi = first_end
        while hi < len(value) and value[hi] != " ":
            hi += 1
    while lo < hi and value[lo] == " ":
        lo += 1
    while hi > lo and value[hi - 1] == " ":
        hi -= 1
    snippet = ("…" if lo else "") + value[lo:hi] + ("…" if hi < len(value) else "")
    flags = [False] * len(snippet)
    shift = int(lo > 0)
    for start, end in matches:
        if lo <= start < end <= hi:
            flags[start - lo + shift:end - lo + shift] = [True] * (end - start)
    return snippet, _offsets(flags)
