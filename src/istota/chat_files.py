"""Which paths `/chat/files` will serve, asked from both sides of the endpoint.

The endpoint lives in `web_app.py`, but the question it answers is also asked
when a task's answer is stored: a reply embedding a `/chat/files` URL the
endpoint would refuse renders as a broken image with nothing to say why
(ISSUE-559). Both callers go through `resolve_chat_file`, so the rule the
browser meets and the rule the scheduler checks against cannot drift.
"""

from __future__ import annotations

import bisect
import html
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.chat_files")


class ChatFileError(Exception):
    """Refusal to serve a path, carrying the status the caller should see."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def chat_file_workspace(config: "Config | None", username: str) -> Path:
    """On-disk root the caller's downloads are confined to.

    Deliberately the user's own workspace with no admin bypass. The endpoint
    exists to hand someone their own files; an admin who needs to read
    elsewhere has the sandbox and the CLI, and widening this would make the
    single most directly-reachable read path on the web app the widest one.
    """
    root = config.workspace_root(username) if config else None
    if root is None:
        raise ChatFileError(
            503,
            "This deployment has no local workspace mount, so files cannot be "
            "served directly. Use a Nextcloud share link instead.",
        )
    return root


def resolve_chat_file(config: "Config | None", username: str, path: str) -> Path:
    """Map a caller-supplied workspace path to a real file, or refuse.

    Two independent checks, because they catch different escapes: the lexical
    scope check (shared with the skill CLI, so the browser and the model are
    held to one rule) rejects ``..`` and absolute paths outside the workspace,
    and the realpath check afterwards rejects a symlink *inside* the workspace
    that points out of it — which no amount of string normalization can see.
    """
    from .nextcloud._http import (
        PathScopeError,
        resolve_scoped_path,
        workspace_root as nc_workspace_root,
    )

    raw = (path or "").strip()
    if not raw:
        raise ChatFileError(400, "path is required")
    if "\x00" in raw:
        raise ChatFileError(400, "path is not a valid filename")

    try:
        # is_admin=False always — see chat_file_workspace.
        scoped = resolve_scoped_path(raw, username, is_admin=False)
    except PathScopeError as e:
        raise ChatFileError(403, str(e)) from e

    root = chat_file_workspace(config, username)
    # Same helper the scope check anchors on, so the Nextcloud-path prefix and
    # the on-disk root can't drift apart.
    relative = scoped[len(nc_workspace_root(username)):].lstrip("/")
    if not relative:
        raise ChatFileError(400, "path names the workspace itself, not a file")

    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, relative))
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ChatFileError(403, "path resolves outside your workspace")

    target = Path(real)
    if not target.exists():
        raise ChatFileError(404, "file not found")
    if target.is_dir():
        raise ChatFileError(400, "path is a directory, not a file")
    if not target.is_file():
        raise ChatFileError(400, "path is not a regular file")
    return target


# A markdown link or image whose destination is the chat-files endpoint. The
# prefix before `/api/chat/files?` is left open — the web UI's base path is a
# deployment setting — but the destination has to be one unbroken token, which
# is also the only shape markdown-it reads as a destination without angle
# brackets.
_CHAT_FILE_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<label>[^\]\n]*)\]"
    r"\((?P<url>[^\s()]*/api/chat/files\?[^\s()]*)\)"
)

# A fixed phrase per refusal rather than the resolver's message: that one can
# quote the path, and this text lands in the user's transcript.
_REASONS = {
    400: "not a file",
    403: "outside your workspace",
    404: "file not found",
}


# CommonMark's backslash escape: a backslash before ASCII punctuation.
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_BACKTICK_RUN_RE = re.compile(r"`+")


def _chat_file_path(url: str) -> str | None:
    """The decoded `path` parameter of a same-origin chat-files URL, or None.

    The destination is unescaped the way markdown-it does before the browser
    sees it — backslash escapes, then entities — so a link that loads is not
    read as a different, missing file. An absolute URL answers None: the web
    client draws only its own relative prefix inline, so a link to another
    origin was never a broken image here and is not ours to judge.
    """
    url = html.unescape(_BACKSLASH_ESCAPE_RE.sub(r"\1", url))
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return None
    values = parse_qs(parts.query).get("path")
    if not values:
        return None
    return values[0]


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Sorted, non-overlapping `(start, end)` ranges of fenced blocks and
    inline code spans, which markdown renders as text and a link inside them
    as no link at all.

    Linear in ``text`` on purpose: this runs over model output, and a lazy
    opener-to-closer regex is quadratic on many openers with no closer (the
    shape `llm_json` measured). An unclosed fence runs to the end of the text,
    as it does when rendered.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    fence: str | None = None
    fence_start = 0
    prose_start = 0
    prose: list[tuple[int, int]] = []
    for line in text.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                prose.append((prose_start, pos))
                fence = match.group(1)
                fence_start = pos
        elif (
            match
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not line[match.end():].strip()
        ):
            spans.append((fence_start, pos + len(line)))
            fence = None
            prose_start = pos + len(line)
        pos += len(line)
    if fence is None:
        prose.append((prose_start, pos))
    else:
        spans.append((fence_start, pos))

    for start, end in prose:
        runs = [
            (m.start(), m.end())
            for m in _BACKTICK_RUN_RE.finditer(text, start, end)
        ]
        # Index of the next run of the same length, built right to left so
        # pairing stays linear however many runs go unclosed.
        next_same: list[int | None] = [None] * len(runs)
        last_by_len: dict[int, int] = {}
        for i in range(len(runs) - 1, -1, -1):
            length = runs[i][1] - runs[i][0]
            next_same[i] = last_by_len.get(length)
            last_by_len[length] = i
        i = 0
        while i < len(runs):
            closer = next_same[i]
            if closer is None:
                i += 1
                continue
            spans.append((runs[i][0], runs[closer][1]))
            i = closer + 1
    spans.sort()
    return spans


def _inside(spans: list[tuple[int, int]], starts: list[int], index: int) -> bool:
    at = bisect.bisect_right(starts, index) - 1
    return at >= 0 and spans[at][0] <= index < spans[at][1]


def check_chat_file_links(
    config: "Config", user_id: str, text: str, *, task_id: int | None = None,
) -> str:
    """Rewrite every `/chat/files` link in ``text`` the endpoint would refuse.

    A refused image becomes its alt text plus a plain `(image unavailable: …)`
    note, and a refused link its label plus `(file unavailable: …)`, so the
    reader sees what is missing instead of a broken-image icon. Each rewrite
    is logged at WARNING with the task id and the refused path, because the
    task itself finished `completed` and nothing else records it.

    Checked against ``user_id``'s workspace, which is the task owner's: a file
    only its owner can fetch is still a file that exists. Nothing is rewritten
    where the endpoint's answer is not a refusal of the file — no local
    workspace mount (the 503) — nor where the link is not one the web client
    would draw: an absolute URL, a URL with no `path`, or anything inside a
    fenced block or inline code, which renders as text. Never raises.
    """
    if not text or "/api/chat/files?" not in text:
        return text

    spans = _code_spans(text)
    starts = [start for start, _ in spans]

    def _replace(match: re.Match) -> str:
        if _inside(spans, starts, match.start()):
            return match.group(0)
        url = match.group("url")
        path = _chat_file_path(url)
        if path is None:
            return match.group(0)
        try:
            resolve_chat_file(config, user_id, path)
            return match.group(0)
        except ChatFileError as e:
            reason = _REASONS.get(e.status)
            if reason is None:
                return match.group(0)
        except Exception:
            logger.exception(
                "chat file link check failed for task %s", task_id,
            )
            return match.group(0)
        is_image = bool(match.group("bang"))
        logger.warning(
            "task %s: %s link to %r would be refused by /chat/files (%s); "
            "rewritten as unavailable",
            task_id, "image" if is_image else "file", path, reason,
        )
        label = match.group("label").strip()
        kind = "image" if is_image else "file"
        note = f"({kind} unavailable: {reason})"
        return f"{label} {note}" if label else note

    try:
        return _CHAT_FILE_LINK_RE.sub(_replace, text)
    except Exception:
        logger.exception("chat file link check failed for task %s", task_id)
        return text
