"""Turning a change into a new config document, and showing it.

`diff_documents` is the one diff of two config documents, used by
`istota apply`'s printed plan and by the editor's preview.
"""

from __future__ import annotations

import difflib

from istota.config_editor import secrets
from istota.lib import toml_write


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
