"""File tools: Read, Write, Edit, Grep, Glob.

Grep and Glob are pure-Python (``re`` + ``pathlib``) rather than shelling out
to ripgrep — no external binary dependency, deterministic across platforms, and
correct for the native brain's needs. Ripgrep's .gitignore awareness and raw
speed are nice-to-haves we can add later if a real corpus demands it.

All file reads/writes run on a worker thread (``asyncio.to_thread``) so a large
file can't stall the event loop driving the agent.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema

from .env import ToolEnv, ToolPathError

_BINARY_SNIFF_BYTES = 8192
_MAX_LINE_CHARS = 2000


def _text(s: str) -> list[TextContent]:
    return [TextContent(text=s)]


def _ok(s: str) -> ToolResult:
    return ToolResult(content=_text(s))


def _err(s: str) -> ToolResult:
    # is_error=True so ToolEndEvent.success and the persisted trace reflect a
    # self-reported failure (file not found, bad regex, path outside workspace)
    # without the tool having to raise (NB-19).
    return ToolResult(content=_text(s), is_error=True)


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def _read_bytes_capped(path: Path, cap: int) -> tuple[bytes, bool]:
    """Read up to ``cap`` bytes. Returns ``(data, truncated)``. Bounds the read
    so a multi-GB file can't OOM the worker before line caps apply (NB-19)."""
    if cap <= 0:
        return path.read_bytes(), False
    with path.open("rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        return data[:cap], True
    return data, False


def _safe_mtime(p: Path) -> float:
    """``st_mtime`` or 0 if the path vanished between listing and stat (NB-19)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def make_read_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Read",
        description=(
            "Read a file from the filesystem. Returns content with line numbers "
            "in `cat -n` format. Use `offset`/`limit` to page through large files."
        ),
        parameters=[
            ToolParameter(name="file_path", type="string", description="Absolute path to the file."),
            ToolParameter(name="offset", type="integer", description="1-based first line to read.", required=False),
            ToolParameter(name="limit", type="integer", description="Max lines to read.", required=False),
        ],
    )

    def _read(args: dict) -> ToolResult:
        try:
            path = env.resolve(args["file_path"])
        except ToolPathError as exc:
            return _err(str(exc))
        if not path.exists():
            return _err(f"File not found: {path}")
        if path.is_dir():
            return _err(f"Path is a directory, not a file: {path}")
        raw, byte_truncated = _read_bytes_capped(path, env.max_read_bytes)
        if _looks_binary(raw):
            return _err(f"Cannot read binary file: {path} ({len(raw)} bytes)")

        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        if byte_truncated:
            lines.append(f"… [file exceeds {env.max_read_bytes} bytes; read was truncated]")
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args.get("limit") or env.max_read_lines)
        start = offset - 1
        selected = lines[start : start + limit]

        if not selected:
            return _ok(f"(file has {len(lines)} lines; offset {offset} is past the end)")

        out_lines = []
        for i, line in enumerate(selected, start=offset):
            if len(line) > _MAX_LINE_CHARS:
                line = line[:_MAX_LINE_CHARS] + "… [line truncated]"
            out_lines.append(f"{i:6d}\t{line}")
        body = "\n".join(out_lines)
        if start + limit < len(lines):
            body += f"\n… ({len(lines) - (start + limit)} more lines; raise `limit` or use `offset`)"
        return _ok(body)

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_read, args)

    return AgentTool(schema=schema, execute=_execute, execution_mode="parallel")


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def make_write_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Write",
        description="Write (or overwrite) a file with the given content. Creates parent directories.",
        parameters=[
            ToolParameter(name="file_path", type="string", description="Absolute path to write."),
            ToolParameter(name="content", type="string", description="Full file content."),
        ],
    )

    def _write(args: dict) -> ToolResult:
        try:
            path = env.resolve(args["file_path"], write=True)
        except ToolPathError as exc:
            return _err(str(exc))
        content = args.get("content", "")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        verb = "Updated" if existed else "Created"
        n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return _ok(f"{verb} {path} ({n} lines, {len(content)} bytes)")

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_write, args)

    return AgentTool(schema=schema, execute=_execute, execution_mode="sequential")


# --------------------------------------------------------------------------- #
# Edit
# --------------------------------------------------------------------------- #


def make_edit_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Edit",
        description=(
            "Replace an exact string in a file. `old_string` must be unique unless "
            "`replace_all` is true. Fails if `old_string` is absent or ambiguous."
        ),
        parameters=[
            ToolParameter(name="file_path", type="string", description="Absolute path to edit."),
            ToolParameter(name="old_string", type="string", description="Exact text to replace."),
            ToolParameter(name="new_string", type="string", description="Replacement text."),
            ToolParameter(
                name="replace_all",
                type="boolean",
                description="Replace every occurrence (default false).",
                required=False,
            ),
        ],
    )

    def _edit(args: dict) -> ToolResult:
        try:
            path = env.resolve(args["file_path"], write=True)
        except ToolPathError as exc:
            return _err(str(exc))
        old = args["old_string"]
        new = args["new_string"]
        replace_all = bool(args.get("replace_all", False))

        if not path.exists():
            return _err(f"File not found: {path}")
        if old == new:
            return _err("old_string and new_string are identical; nothing to do.")

        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return _err(f"old_string not found in {path}.")
        if count > 1 and not replace_all:
            return _err(
                f"old_string occurs {count} times in {path}; pass replace_all=true "
                "or include more surrounding context to make it unique."
            )

        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        where = f"{count} occurrences" if replace_all else "1 occurrence"
        return _ok(f"Edited {path} ({where} replaced).")

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_edit, args)

    return AgentTool(schema=schema, execute=_execute, execution_mode="sequential")


# --------------------------------------------------------------------------- #
# Glob
# --------------------------------------------------------------------------- #


def make_glob_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Glob",
        description=(
            "Find files matching a glob pattern (e.g. `**/*.py`), newest first. "
            "Searches under `path` (defaults to the working directory)."
        ),
        parameters=[
            ToolParameter(name="pattern", type="string", description="Glob pattern."),
            ToolParameter(name="path", type="string", description="Root directory to search.", required=False),
        ],
    )

    def _glob(args: dict) -> ToolResult:
        try:
            root = env.resolve(args["path"]) if args.get("path") else env.cwd
        except ToolPathError as exc:
            return _err(str(exc))
        if not root.exists():
            return _err(f"Search path not found: {root}")
        pattern = args["pattern"]
        matches = [p for p in root.glob(pattern) if p.is_file() and env.contains(p)]
        matches.sort(key=_safe_mtime, reverse=True)
        if not matches:
            return _ok(f"No files match {pattern!r} under {root}.")
        listing = "\n".join(str(p) for p in matches[:200])
        if len(matches) > 200:
            listing += f"\n… ({len(matches) - 200} more)"
        return _ok(listing)

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_glob, args)

    return AgentTool(schema=schema, execute=_execute, execution_mode="parallel")


# --------------------------------------------------------------------------- #
# Grep
# --------------------------------------------------------------------------- #


def make_grep_tool(env: ToolEnv) -> AgentTool:
    schema = ToolSchema(
        name="Grep",
        description=(
            "Search file contents with a regular expression. `output_mode` is "
            "`files_with_matches` (default), `content`, or `count`. Restrict scope "
            "with `path` and `glob`; `-i` for case-insensitive."
        ),
        parameters=[
            ToolParameter(name="pattern", type="string", description="Regular expression."),
            ToolParameter(name="path", type="string", description="File or directory to search.", required=False),
            ToolParameter(name="glob", type="string", description="Filter files by glob, e.g. `*.py`.", required=False),
            ToolParameter(
                name="output_mode",
                type="string",
                description="files_with_matches | content | count",
                required=False,
                enum=["files_with_matches", "content", "count"],
            ),
            ToolParameter(name="-i", type="boolean", description="Case-insensitive.", required=False),
            ToolParameter(name="head_limit", type="integer", description="Cap result lines.", required=False),
        ],
    )

    def _iter_files(root: Path, glob_filter: str | None):
        if root.is_file():
            yield root
            return
        # A glob with a path separator is matched against the file's path
        # relative to the root (fnmatch's `*` spans `/`, so `src/**/*.py` works);
        # a bare glob matches the basename (NB-19). Previously only the basename
        # was ever matched, so any path-glob silently found nothing.
        path_glob = bool(glob_filter and "/" in glob_filter)
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip the usual noise so the native grep isn't drowned by VCS/venv.
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
            for name in filenames:
                fpath = Path(dirpath) / name
                if glob_filter:
                    if path_glob:
                        try:
                            rel = fpath.relative_to(root).as_posix()
                        except ValueError:
                            rel = fpath.as_posix()
                        if not fnmatch.fnmatch(rel, glob_filter):
                            continue
                    elif not fnmatch.fnmatch(name, glob_filter):
                        continue
                yield fpath

    def _grep(args: dict) -> ToolResult:
        try:
            root = env.resolve(args["path"]) if args.get("path") else env.cwd
        except ToolPathError as exc:
            return _err(str(exc))
        if not root.exists():
            return _err(f"Search path not found: {root}")
        flags = re.IGNORECASE if args.get("-i") else 0
        try:
            rx = re.compile(args["pattern"], flags)
        except re.error as exc:
            return _err(f"Invalid regex: {exc}")

        mode = args.get("output_mode") or "files_with_matches"
        head_limit = args.get("head_limit")
        glob_filter = args.get("glob")

        matched_files: list[str] = []
        content_lines: list[str] = []
        per_file_counts: dict[str, int] = {}

        for fpath in _iter_files(root, glob_filter):
            if not env.contains(fpath):
                continue
            try:
                raw, _ = _read_bytes_capped(fpath, env.max_read_bytes)
            except OSError:
                continue
            if _looks_binary(raw):
                continue
            file_hit = False
            for lineno, line in enumerate(raw.decode("utf-8", "replace").splitlines(), start=1):
                if rx.search(line):
                    file_hit = True
                    per_file_counts[str(fpath)] = per_file_counts.get(str(fpath), 0) + 1
                    if mode == "content":
                        snippet = line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…"
                        content_lines.append(f"{fpath}:{lineno}:{snippet}")
            if file_hit:
                matched_files.append(str(fpath))

        if mode == "count":
            if not per_file_counts:
                return _ok("No matches.")
            lines = [f"{path}:{count}" for path, count in sorted(per_file_counts.items())]
            return _ok("\n".join(_apply_head(lines, head_limit)))
        if mode == "content":
            if not content_lines:
                return _ok("No matches.")
            return _ok("\n".join(_apply_head(content_lines, head_limit)))
        # files_with_matches
        if not matched_files:
            return _ok("No matches.")
        return _ok("\n".join(_apply_head(sorted(matched_files), head_limit)))

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_grep, args)

    return AgentTool(schema=schema, execute=_execute, execution_mode="parallel")


def _apply_head(lines: list[str], head_limit) -> list[str]:
    if head_limit and len(lines) > int(head_limit):
        kept = lines[: int(head_limit)]
        kept.append(f"… ({len(lines) - int(head_limit)} more)")
        return kept
    return lines
