"""Turning a change into a new config document, and showing it.

`diff_documents` is the one diff of two config documents, used by
`istota apply`'s printed plan and by the editor's preview. `has_comments` is
what the view warns on, since every write goes through `toml_write`.
"""

from __future__ import annotations

import difflib

from istota.config_editor import secrets
from istota.lib import toml_write


def has_comments(text: str) -> bool:
    """Whether TOML `text` holds a comment, which a rewrite through `toml_write` drops.

    A `#` inside a string is not one, so strings are skipped: basic and literal,
    single- and multi-line, with a basic string's escapes.
    """
    index, size = 0, len(text)
    quote: str | None = None
    while index < size:
        if quote is None:
            if text.startswith('"""', index) or text.startswith("'''", index):
                quote = text[index:index + 3]
                index += 3
                continue
            char = text[index]
            if char == "#":
                return True
            if char in "\"'":
                quote = char
            index += 1
            continue
        if quote[0] == '"' and text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote, index):
            index += len(quote)
            quote = None
            continue
        if len(quote) == 1 and text[index] == "\n":
            quote = None
        index += 1
    return False


def diff_documents(
    current: dict, new: dict, *, fromfile: str = "a/config.toml", tofile: str = "b/config.toml",
) -> list[str]:
    """A unified diff of two documents, as lines without their newlines.

    Both are redacted before they are rendered (`secrets.redact_pair`), rather
    than lines of a rendered diff being redacted by key name, which let a
    header value under `[brain.native.extra_headers]` through. Both sides go
    through `toml_write`, so a hand-written file's formatting and comments make
    no diff lines.
    """
    current_redacted, new_redacted = secrets.redact_pair(current, new)
    return [
        line.rstrip("\n")
        for line in difflib.unified_diff(
            toml_write.dumps(current_redacted).splitlines(keepends=True),
            toml_write.dumps(new_redacted).splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    ]
