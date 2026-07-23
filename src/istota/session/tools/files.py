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
import json
import os
import re
from pathlib import Path

from istota.agent.coercion import coerce_arguments
from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import TextContent, ToolParameter, ToolSchema

from .edit_engine import (
    Edit,
    EditError,
    apply_edits_to_normalized_content,
    detect_line_ending,
    normalize_to_lf,
    restore_line_endings,
    strip_bom,
)
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
            remaining = len(lines) - (start + limit)
            next_offset = offset + limit
            body += f"\n… ({remaining} more lines; continue with offset={next_offset})"
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
            "Replace exact text in a file. Pass a single `old_string`/`new_string` "
            "(must be unique unless `replace_all` is true), or an `edits` array of "
            "`{old_string, new_string}` to make several disjoint changes in one "
            "call. Matching tolerates trailing-whitespace and smart-quote/dash "
            "drift; it does not tolerate indentation changes."
        ),
        parameters=[
            ToolParameter(name="file_path", type="string", description="Absolute path to edit."),
            ToolParameter(
                name="old_string", type="string", description="Exact text to replace.", required=False
            ),
            ToolParameter(
                name="new_string", type="string", description="Replacement text.", required=False
            ),
            ToolParameter(
                name="edits",
                type="array",
                description="Array of {old_string, new_string} for multiple disjoint edits.",
                required=False,
                # Declared so strict providers (Google Gemini) accept the
                # array — an undeclared ``items`` is ``INVALID_ARGUMENT``.
                items=ToolParameter(
                    name="edit",
                    type="object",
                    description="A single find-and-replace edit.",
                    required=False,
                    properties={
                        "old_string": ToolParameter(
                            name="old_string",
                            type="string",
                            description="Exact text to find.",
                            required=False,
                        ),
                        "new_string": ToolParameter(
                            name="new_string",
                            type="string",
                            description="Replacement text.",
                            required=False,
                        ),
                    },
                ),
            ),
            ToolParameter(
                name="replace_all",
                type="boolean",
                description="Replace every occurrence (exact match only; ignored with `edits`).",
                required=False,
            ),
        ],
    )

    def _edit(args: dict) -> ToolResult:
        try:
            path = env.resolve(args["file_path"], write=True)
        except ToolPathError as exc:
            return _err(str(exc))
        if not path.exists():
            return _err(f"File not found: {path}")

        replace_all = bool(args.get("replace_all", False))
        edits = args.get("edits")

        # replace_all keeps the legacy exact-only semantics (fuzzy + replace_all
        # is ambiguous — see the spec). The shim never routes a replace_all call
        # through the batch path, so `edits` is None here when replace_all is set.
        if replace_all:
            old = args.get("old_string")
            new = args.get("new_string")
            if old is None or new is None:
                return _err("old_string and new_string are required.")
            if old == new:
                return _err("old_string and new_string are identical; nothing to do.")
            text = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                return _err(f"old_string not found in {path}.")
            path.write_text(text.replace(old, new), encoding="utf-8")
            return _ok(f"Edited {path} ({count} occurrences replaced).")

        # Legacy single-edit shape → one-element batch. The prepare shim does
        # this too; repeating it here keeps _edit correct if called directly
        # (tests) or if a caller skips the shim.
        if not edits and args.get("old_string") is not None and args.get("new_string") is not None:
            edits = [{"old_string": args["old_string"], "new_string": args["new_string"]}]
        if not edits:
            return _err("Provide old_string/new_string or a non-empty edits array.")
        if not isinstance(edits, list):
            return _err("edits must be an array of {old_string, new_string} objects.")
        try:
            edit_objs = [
                Edit(old_string=e["old_string"], new_string=e["new_string"]) for e in edits
            ]
        except (TypeError, KeyError):
            return _err("Each edit must be an object with old_string and new_string.")

        # Read raw bytes (not read_text) so universal-newline translation can't
        # strip CRLF before we detect + preserve it.
        raw = path.read_bytes().decode("utf-8")
        bom, without_bom = strip_bom(raw)
        ending = detect_line_ending(without_bom)
        normalized = normalize_to_lf(without_bom)
        try:
            applied = apply_edits_to_normalized_content(normalized, edit_objs, str(path))
        except EditError as exc:
            return _err(str(exc))

        out = bom + restore_line_endings(applied.new_content, ending)
        path.write_bytes(out.encode("utf-8"))
        n = len(edit_objs)
        return _ok(f"Edited {path} ({n} block(s) replaced).")

    def _prepare(args: dict) -> dict:
        """Coerce arg types, then the two shapes weaker models emit for a
        multi-edit call:

        - ``edits`` sent as a JSON *string* → parsed to a list (coercion does
          this via the ``array`` param type).
        - Legacy top-level ``{old_string, new_string}`` with no ``edits`` and no
          ``replace_all`` → synthesize a one-element ``edits`` batch so a single
          legacy call flows through the same fuzzy engine as a batch.
          ``replace_all`` stays on the exact single-edit path (fuzzy +
          replace_all is disallowed).
        """
        out = coerce_arguments(args, schema)
        if (
            out.get("edits") is None
            and not out.get("replace_all")
            and out.get("old_string") is not None
            and out.get("new_string") is not None
        ):
            out["edits"] = [{"old_string": out["old_string"], "new_string": out["new_string"]}]
        return out

    async def _execute(call_id, args, on_update, abort):
        return await asyncio.to_thread(_edit, args)

    return AgentTool(
        schema=schema,
        execute=_execute,
        execution_mode="sequential",
        prepare_arguments=_prepare,
    )


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
            "with `path` and `glob`; `-i` for case-insensitive; `-C` for context "
            "lines around each match (content mode); `literal` to match `pattern` "
            "as a plain string."
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
            ToolParameter(
                name="-C",
                type="integer",
                description="Lines of context around each match (content mode only).",
                required=False,
            ),
            ToolParameter(
                name="literal",
                type="boolean",
                description="Treat `pattern` as a literal string, not a regex.",
                required=False,
            ),
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
        pattern = args["pattern"]
        if args.get("literal"):
            pattern = re.escape(pattern)
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            return _err(f"Invalid regex: {exc}")

        mode = args.get("output_mode") or "files_with_matches"
        head_limit = args.get("head_limit")
        glob_filter = args.get("glob")
        # -C / context: lines of surrounding context in content mode.
        context = int(args.get("-C") or args.get("context") or 0)

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
            file_lines = raw.decode("utf-8", "replace").splitlines()
            match_linenos = [i for i, line in enumerate(file_lines, start=1) if rx.search(line)]
            if not match_linenos:
                continue
            per_file_counts[str(fpath)] = len(match_linenos)
            matched_files.append(str(fpath))
            if mode == "content":
                if context > 0:
                    # `--` group separator between files (and, inside the render,
                    # between non-adjacent hit groups).
                    if content_lines:
                        content_lines.append("--")
                    content_lines.extend(
                        _render_content_with_context(fpath, file_lines, match_linenos, context)
                    )
                else:
                    for lineno in match_linenos:
                        line = file_lines[lineno - 1]
                        snippet = line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…"
                        content_lines.append(f"{fpath}:{lineno}:{snippet}")

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


def _render_content_with_context(
    fpath: Path, file_lines: list[str], match_linenos: list[int], context: int
) -> list[str]:
    """Render matches with ``context`` surrounding lines (ripgrep convention).

    Match lines use ``path:lineno:text``, context lines ``path-lineno-text``;
    a ``--`` separator marks a gap between non-adjacent groups of shown lines."""
    match_set = set(match_linenos)
    n = len(file_lines)
    shown: set[int] = set()
    for m in match_linenos:
        for ln in range(max(1, m - context), min(n, m + context) + 1):
            shown.add(ln)

    out: list[str] = []
    prev: int | None = None
    for ln in sorted(shown):
        if prev is not None and ln != prev + 1:
            out.append("--")
        line = file_lines[ln - 1]
        snippet = line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…"
        sep = ":" if ln in match_set else "-"
        out.append(f"{fpath}{sep}{ln}{sep}{snippet}")
        prev = ln
    return out


def _apply_head(lines: list[str], head_limit) -> list[str]:
    if head_limit and len(lines) > int(head_limit):
        kept = lines[: int(head_limit)]
        kept.append(
            f"… ({len(lines) - int(head_limit)} more; raise head_limit or narrow path/glob)"
        )
        return kept
    return lines
