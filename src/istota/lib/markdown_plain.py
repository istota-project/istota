"""Markdown flattened to the text a reader would see, for plain-text previews.

Not a parser: a line pass for block markers, then an inline pass for spans.
Code spans, backslash escapes and URLs are set aside before emphasis is
stripped, so their contents come back verbatim. Underscore emphasis needs a
non-word character on both sides, so `snake_case_name` keeps its underscores.
Line breaks are kept; callers collapse them if they want one line.
"""

import re

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_SETEXT = re.compile(r"^\s{0,3}=+[ \t]*$")
# No two unbounded runs of the same class sit side by side, so a long line of
# spaces fails in linear time rather than quadratic.
_TABLE_RULE = re.compile(r"^[ \t]*(?:\|[ \t]*)?:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+(?:\|[ \t]*)?$")
_QUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
_TASK_BOX = re.compile(r"^\[[ xX]\]\s+")

_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^<>\s]+)>")
_BARE_URL = re.compile(r"https?://[^\s<>()\[\]]+")
_IMAGE = re.compile(r"!\[([^\[\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\[\]]+)\]\((?:[^()]|\([^()]*\))*\)")
_REF_LINK = re.compile(r"\[([^\[\]]+)\]\[[^\[\]]*\]")
# Known tags only, so a `<placeholder>` or `x<y and y>z` in prose keeps its words.
_HTML_TAG = re.compile(
    r"</?(?:a|abbr|b|br|code|del|details|div|em|hr|i|img|ins|kbd|mark|p|pre|s|small"
    r"|span|strong|sub|summary|sup|u)(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)
_EMPHASIS = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
    re.compile(r"==(?=\S)(.+?)(?<=\S)=="),
    re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*"),
    re.compile(r"(?<!\w)_(?=\S)([^_]+?)(?<=\S)_(?!\w)"),
)
_MARK = "\x00"
_KEPT = re.compile(_MARK + r"(\d+)" + _MARK)


def _inline(line: str) -> str:
    kept: list[str] = []

    def keep(value: str) -> str:
        kept.append(value)
        return f"{_MARK}{len(kept) - 1}{_MARK}"

    line = _CODE_SPAN.sub(lambda m: keep(m.group(2).strip()), line)
    line = _ESCAPE.sub(lambda m: keep(m.group(1)), line)
    line = _AUTOLINK.sub(lambda m: keep(m.group(1)), line)
    line = _IMAGE.sub(r"\1", line)
    line = _LINK.sub(r"\1", line)
    line = _REF_LINK.sub(r"\1", line)
    # After links, so a destination is dropped whole rather than kept as text.
    line = _BARE_URL.sub(lambda m: keep(m.group(0)), line)
    line = _HTML_TAG.sub("", line)
    for pattern in _EMPHASIS:
        line = pattern.sub(r"\1", line)
    return _KEPT.sub(lambda m: kept[int(m.group(1))], line)


def _strip_closing_hashes(line: str) -> str:
    line = line.rstrip()
    bare = line.rstrip("#")
    if bare != line and (not bare or bare[-1] in " \t"):
        return bare.rstrip()
    return line


def _table_row(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return " · ".join(cell for cell in cells if cell)


def to_plain_text(text: str) -> str:
    """Return ``text`` with its Markdown syntax removed and its words kept."""
    lines: list[str] = []
    fence: str | None = None
    for line in str(text or "").replace(_MARK, "").splitlines():
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) \
                    and not line[opener.end():].strip():
                fence = None
            else:
                lines.append(line)
            continue
        if opener:
            fence = opener.group(1)
            continue
        if _RULE.match(line) or _SETEXT.match(line) or _TABLE_RULE.match(line):
            continue
        row = _table_row(line)
        if row is not None:
            lines.append(_inline(row))
            continue
        line = _QUOTE.sub("", line)
        if _HEADING.match(line):
            line = _strip_closing_hashes(_HEADING.sub("", line))
        else:
            line = _TASK_BOX.sub("", _LIST.sub("", line))
        lines.append(_inline(line))
    return "\n".join(lines).strip()
