"""Descriptions and limits for the user's scripts inventory."""

import re

DESCRIPTION_HEAD_BYTES = 8192
DESCRIPTION_MAX_CHARS = 200
SCRIPTS_MAX_DEPTH = 4
SCRIPTS_MAX_FILES = 1000

_PREAMBLE = re.compile(
    r"#\s*(?:(?:-\*-\s*)?coding[:=]\s*[-\w.]+|shellcheck\b|noqa\b|type:|pyright:|vim:)"
    r"|set\s+(?:-[a-zA-Z]+(?:\s+pipefail)?)(?:\s*;)?\s*$"
)
_DOCSTRING = re.compile(r"[rRuU]?(\"\"\"|''')")


def _summary(text: str) -> str | None:
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        return text[:DESCRIPTION_MAX_CHARS] + "…"
    return text or None


def describe(name: str, head: bytes, *, kind: str) -> str | None:
    """Read only the leading description, never comments below code.

    The filename does not select a language: every script may use a comment
    or a Python-style docstring. Prompt files use their first non-empty line.
    """
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\0" in text:
        return None
    lines = iter(text.splitlines())
    if kind == "prompt":
        for line in lines:
            if line.strip():
                return _summary(line.strip().lstrip("#"))
        return None

    comment_started = False
    for index, line in enumerate(lines):
        if index == 0 and line.startswith("#!"):
            continue
        line = line.strip()
        if not comment_started and (not line or _PREAMBLE.match(line)):
            continue
        for marker in ("#", "//", "--"):
            if line.startswith(marker):
                content = line[len(marker):].strip()
                if marker == "#" and not content.strip("#"):
                    content = ""
                if content:
                    return _summary(content)
                comment_started = True
                break
        else:
            if comment_started:
                return None
            opening = _DOCSTRING.match(line)
            if opening is None:
                return None
            quotes = opening.group(1)
            content = line[opening.end():]
            while True:
                description, closing, _ = content.partition(quotes)
                if description.strip():
                    return _summary(description)
                if closing:
                    return None
                content = next(lines, None)
                if content is None:
                    return None
    return None
