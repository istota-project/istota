"""Descriptions, references and limits for the user's scripts inventory."""

import re
from pathlib import Path, PurePosixPath

DESCRIPTION_HEAD_BYTES = 8192
DESCRIPTION_MAX_CHARS = 200
SCRIPTS_MAX_DEPTH = 4
SCRIPTS_MAX_FILES = 1000

_PREAMBLE = re.compile(
    r"#\s*(?:(?:-\*-\s*)?coding[:=]\s*[-\w.]+|shellcheck\b|noqa\b|type:|pyright:|vim:)"
    r"|set\s+(?:-[a-zA-Z]+(?:\s+pipefail)?)(?:\s*;)?\s*$"
)
_DOCSTRING = re.compile(r"[rRuU]?(\"\"\"|''')")
_REFERENCE_START = r"(?<![^\s'\"`;|&<>()=])"
_REFERENCE_TAIL = r"([^\s'\"`;|&<>()]+)"


def _matched_script_paths(text: str, prefix_pattern: str) -> list[str]:
    paths = set()
    pattern = _REFERENCE_START + prefix_pattern + _REFERENCE_TAIL
    for match in re.finditer(pattern, text):
        raw = match.group(1)
        # Reject traversal before stripping punctuation: a final '..' is
        # a path component too, not a sentence-ending period.
        if re.search(r"(?:^|/)\.\.(?:/|[.,:]*$)", raw):
            continue
        path = PurePosixPath(raw.rstrip(".,:").lstrip("/"))
        if path.parts:
            paths.add(path.as_posix())
    return sorted(paths)


def find_script_paths(
    text: str, *, user_id: str, bot_dir: str, workspace_path: Path,
) -> list[str]:
    """Extract relative script paths from the user's absolute path forms.

    Like maintenance.workspace_paths._PATH_END, shell quotes and punctuation
    delimit paths. Here slashes stay inside the extracted relative path;
    trailing prose punctuation is stripped after extraction. Roots must start
    at a shell boundary so a longer, unrelated root cannot match by suffix.
    """
    roots = {
        str(workspace_path).rstrip("/"),
        str(workspace_path.resolve()).rstrip("/"),
        "$ISTOTA_WORKSPACE_PATH", "${ISTOTA_WORKSPACE_PATH}",
        "$NEXTCLOUD_MOUNT_PATH", "${NEXTCLOUD_MOUNT_PATH}",
    }
    root_pattern = "|".join(re.escape(root) for root in sorted(roots))
    prefix = re.escape(f"/Users/{user_id}/{bot_dir}/scripts/")
    return _matched_script_paths(text, f"(?:{root_pattern})?{prefix}")


def find_bare_mentions(text: str, existing: set[str]) -> list[str]:
    """Find bare scripts/ paths in prompts, limited to known files."""
    return [path for path in _matched_script_paths(text, "scripts/") if path in existing]


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
