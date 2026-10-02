"""Where a ```toml fence starts and ends, written down once (ISSUE-386).

Four modules parse a TOML block out of a markdown file the *user* writes —
``cron_loader`` (CRON.md), ``heartbeat`` (HEARTBEAT.md), ``user_briefings``
(BRIEFINGS.md) and ``money._config_io`` (the module's UPPERCASE.md configs).
All four carried the same expression by copy, ``r"```toml\\s*\\n(.*?)```"``,
and all four had the same defect: neither marker was anchored to a line, so
the block ended at the first backtick run appearing anywhere after the fence
opened — inside a comment, inside a string value.

What that costs differs per caller, which is why it was only ever found in
one of them. ``cron_loader`` drives an orphan sweep, so a truncation that
happened to land on a table boundary produced *valid TOML holding a subset
of the jobs* and the sweep deleted the rest, silently and for good. The other
three degrade instead: a heartbeat check or a briefing entry below the
truncation simply stops existing. Same bug, and the reason it lives here now
is that a fifth copy would inherit it again.

**Every bound is loose on purpose.** The expression this replaces had no
``^`` at all, so it accepted any prefix — which makes almost any bound a
narrowing, and a narrowing is what breaks a file that used to work. So the
indent is unbounded rather than CommonMark's three spaces, a marker is
``{3,}`` backticks on either side, and the trailing class is ``[^\\S\\n]`` —
every whitespace except a newline, which is what carries the ``\\r`` of a
CRLF file and the non-breaking space a paste from a rendered page leaves
behind. A leading BOM is named separately because it is not ``\\s``. None of
that weakens the fix: what closes ISSUE-386 is that a marker must be alone
on its line, and it still must be.

**Two searches rather than one expression, and that is not a refactor.** The
obvious ``open(.*?)close`` form is quadratic in the file when no closer
matches: every opener is a fresh start position and each rescans to EOF.
Measured on the combined form, 64 KB of repeated openers took 4.2s and
256 KB took 65.8s. ``cron_loader``'s caller runs on the scheduler's own tick
with no timeout, and these files are user-writable over the mount, so that
is a denial of service rather than a slow parse.

What this module deliberately does **not** own is what a caller does when it
finds no block. That answer is not shared: for ``cron_loader`` a file it
cannot resolve must never read as "the user has authored no jobs", because
that verdict authorizes a branch that rewrites the whole document — so it
uses the markers below directly and keeps its own hold guard. The other
three have no such branch and take :func:`find_toml_block`.

stdlib-only leaf; imports nothing from the package.
"""

import re

# Whitespace that is not a line break, which is what carries a CRLF's `\r`
# and a pasted non-breaking space.
_FENCE_INDENT = r"[^\S\n]*"
_FENCE_TICKS = r"`{3,}"

# Nothing may precede either marker on its line but whitespace (and, at the
# very start of the file, a BOM — Notepad writes one and it is not `\s`).
FENCE_OPEN_RE = re.compile(
    rf"^﻿?{_FENCE_INDENT}{_FENCE_TICKS}toml[^\n]*\n", re.MULTILINE,
)
FENCE_CLOSE_RE = re.compile(
    rf"^{_FENCE_INDENT}{_FENCE_TICKS}[^\S\n]*$", re.MULTILINE,
)

# Any backtick run at all, wherever it sits on its line. Used by a caller
# that needs to tell "this file plausibly holds a fence I could not read"
# from "this file holds no fence", where the second answer is the dangerous
# one to get wrong.
BACKTICK_RUN_RE = re.compile(_FENCE_TICKS)


def find_toml_block(text: str) -> tuple[int, int] | None:
    """The span of the first toml block's *body*, or ``None``.

    Neither marker's own indent is inside the span, so a caller splicing a
    replacement over it writes both markers back exactly as the user
    indented them. ``None`` means no opener, or an opener with no closer —
    a caller needing to tell those apart uses the two expressions directly.
    """
    opener = FENCE_OPEN_RE.search(text)
    if opener is None:
        return None
    closer = FENCE_CLOSE_RE.search(text, opener.end())
    if closer is None:
        return None
    return opener.end(), closer.start()
