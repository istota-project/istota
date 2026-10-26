"""The validator menu for mechanical done-when items, and running one.

Three checks: `file_parses`, `csv_columns` and a literal `contains`. A check
reads a deliverable a wake wrote, in the daemon, so the file is opened through
`sandbox.confined` from `Config.workspace_root(user_id)` with no link followed
at any component, read under a 1 MiB cap, and never matched with a regex
(Python's `re` has no timeout).

`parse_spec` is the one definition of a valid spec: `item add` calls it before
storing, and `run_item` calls it again on the stored spec, so a row written
some other way cannot widen what a check opens. `run_item` never raises; an
unexpected error is a `failing` result with detail `validator error`.

Details are host-written text. They never quote the file's contents, since the
detail reaches the next wake's prompt and the user's notices.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from istota.sandbox import confined

if TYPE_CHECKING:
    from istota.assignments.store import Assignment, Item
    from istota.config import Config

logger = logging.getLogger("istota.assignments.validators")

CHECKS = ("file_parses", "csv_columns", "contains")
FORMATS = ("csv", "json", "toml", "markdown")
MAX_FILE_BYTES = 1024 * 1024
MAX_FILE_PARTS = 4
MAX_COLUMNS = 50
MAX_TEXT_CHARS = 1000

_KEYS = {
    "file_parses": {"check", "file", "format"},
    "csv_columns": {"check", "file", "columns", "min_rows"},
    "contains": {"check", "file", "text", "present"},
}


class InvalidValidator(ValueError):
    """A validator spec outside the menu. The message is safe to show the user."""


@dataclass(frozen=True)
class ItemResult:
    status: str  # "passing" | "failing"
    detail: str


def file_parts(file: object) -> tuple[str, ...]:
    """A spec's `file` as components below the assignment directory."""
    if not isinstance(file, str) or not file:
        raise InvalidValidator("file must be a relative path inside the assignment directory")
    parts = tuple(file.split("/"))
    if len(parts) > MAX_FILE_PARTS:
        raise InvalidValidator(f"file may have at most {MAX_FILE_PARTS} path parts")
    if not confined.valid_parts(parts):
        raise InvalidValidator("file must be a relative path inside the assignment directory")
    return parts


def parse_spec(spec: object) -> dict[str, Any]:
    """The normalized spec, or `InvalidValidator`."""
    if not isinstance(spec, dict):
        raise InvalidValidator("a validator spec is an object")
    check = spec.get("check")
    if check not in CHECKS:
        raise InvalidValidator(f"check must be one of {', '.join(CHECKS)}")
    unknown = sorted(set(spec) - _KEYS[check])
    if unknown:
        raise InvalidValidator(f"{check} does not take {', '.join(unknown)}")
    file_parts(spec.get("file"))
    out: dict[str, Any] = {"check": check, "file": spec["file"]}

    if check == "file_parses":
        if spec.get("format") not in FORMATS:
            raise InvalidValidator(f"format must be one of {', '.join(FORMATS)}")
        out["format"] = spec["format"]

    elif check == "csv_columns":
        columns = spec.get("columns")
        if not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS:
            raise InvalidValidator(f"columns must list between 1 and {MAX_COLUMNS} column names")
        if not all(isinstance(c, str) and c.strip() for c in columns):
            raise InvalidValidator("every column name must be non-empty text")
        min_rows = spec.get("min_rows", 1)
        if isinstance(min_rows, bool) or not isinstance(min_rows, int) or min_rows < 1:
            raise InvalidValidator("min_rows must be a whole number of at least 1")
        out["columns"] = [c.strip() for c in columns]
        out["min_rows"] = min_rows

    else:
        text = spec.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
            raise InvalidValidator(f"text must be between 1 and {MAX_TEXT_CHARS} characters")
        present = spec.get("present", True)
        if not isinstance(present, bool):
            raise InvalidValidator("present must be true or false")
        out["text"] = text
        out["present"] = present

    return out


def describe(spec: dict[str, Any]) -> str:
    """One line for the rendered charter: `csv_columns quotes.csv source,date; min 3 rows`."""
    check = spec.get("check")
    file = spec.get("file", "")
    if check == "file_parses":
        return f"file_parses {file} as {spec.get('format')}"
    if check == "csv_columns":
        return f"csv_columns {file} {','.join(spec.get('columns', []))}; min {spec.get('min_rows', 1)} rows"
    if check == "contains":
        text = json.dumps(spec.get("text", ""), ensure_ascii=False)
        return f"contains {file} {text}" + ("" if spec.get("present", True) else " absent")
    return str(check)


def run_item(config: "Config", assignment: "Assignment", item: "Item") -> ItemResult:
    """Run one mechanical item's check against the files as they are now."""
    try:
        return _run(config, assignment, item)
    except Exception:
        logger.exception(
            "assignment validator error assignment_id=%s item_id=%s", assignment.id, item.id,
        )
        return ItemResult("failing", "validator error")


def run_mechanical(config: "Config", assignment: "Assignment", items) -> dict[int, ItemResult]:
    """Every mechanical item's result, keyed by item id. Reads files, so the
    caller holds no write transaction while it runs."""
    return {item.id: run_item(config, assignment, item) for item in items if item.kind == "mechanical"}


def items_key(items) -> tuple:
    """What a file read depends on in the done-when list: the mechanical items
    and their checks, not their status. A write transaction compares it with
    the list it reads, so results taken before it opened are not applied to a
    list that changed in between."""
    return tuple(
        (item.id, json.dumps(item.validator, sort_keys=True))
        for item in items if item.kind == "mechanical"
    )


def _run(config: "Config", assignment: "Assignment", item: "Item") -> ItemResult:
    if item.kind != "mechanical" or item.validator is None:
        return ItemResult("failing", "not a mechanical item")
    try:
        spec = parse_spec(item.validator)
    except InvalidValidator:
        logger.warning(
            "assignment validator spec refused assignment_id=%s item_id=%s", assignment.id, item.id,
        )
        return ItemResult("failing", "validator error")

    root = config.workspace_root(assignment.user_id)
    if root is None:
        return ItemResult("failing", "no workspace")
    dir_parts = (config.bot_dir_name, *assignment.dir_relpath.split("/"))
    *sub_dirs, leaf = file_parts(spec["file"])

    data, refusal = _read(root, (*dir_parts, *sub_dirs), leaf)
    if refusal is not None:
        return ItemResult("failing", refusal)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ItemResult("failing", "not valid UTF-8")

    if spec["check"] == "file_parses":
        return _file_parses(text, spec["format"])
    if spec["check"] == "csv_columns":
        return _csv_columns(text, spec["columns"], spec["min_rows"])
    found = spec["text"] in text
    if spec["present"]:
        return ItemResult("passing", "text found") if found else ItemResult("failing", "text not found")
    return ItemResult("failing", "text present") if found else ItemResult("passing", "text absent")


def deliverable_digests(config: "Config", assignment: "Assignment", items) -> dict[str, str | None]:
    """sha256 of every file a mechanical item checks, keyed by the spec's `file`.

    None for a file that cannot be read as the checks read it. The host
    compares these around a wake for the `deliverable_changed` signal. Never raises.
    """
    digests: dict[str, str | None] = {}
    root = config.workspace_root(assignment.user_id)
    dir_parts = (config.bot_dir_name, *assignment.dir_relpath.split("/"))
    for item in items:
        if item.kind != "mechanical" or item.validator is None:
            continue
        try:
            file = parse_spec(item.validator)["file"]
            if file in digests:
                continue
            digests[file] = None
            if root is None:
                continue
            *sub_dirs, leaf = file_parts(file)
            data, refusal = _read(root, (*dir_parts, *sub_dirs), leaf)
            if refusal is None:
                digests[file] = hashlib.sha256(data).hexdigest()
        except Exception:
            logger.warning(
                "assignment deliverable digest failed assignment_id=%s item_id=%s",
                assignment.id, item.id,
            )
    return digests


def item_file(item: "Item") -> str | None:
    """The file a mechanical item checks, or None."""
    if item.kind != "mechanical" or not isinstance(item.validator, dict):
        return None
    file = item.validator.get("file")
    return file if isinstance(file, str) else None


def _read(root, dir_parts: tuple[str, ...], leaf: str) -> tuple[bytes | None, str | None]:
    """`(bytes, None)` or `(None, refusal)`, opened by descriptor from `root`."""
    dir_fd = confined.open_dir(root, *dir_parts)
    if dir_fd is None:
        return None, "file missing"
    try:
        fd = confined.open_regular_at(dir_fd, leaf)
        if fd is None:
            try:
                os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None, "file missing"
            except OSError:
                pass
            return None, "not a regular file"
    finally:
        os.close(dir_fd)
    try:
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_FILE_BYTES:
        return None, "file larger than 1 MiB"
    return b"".join(chunks), None


def _csv_rows(text: str) -> tuple[list[tuple[int, list[str]]] | None, str | None]:
    """`(line, row)` for each non-blank row, one column count throughout, or a refusal."""
    rows: list[tuple[int, list[str]]] = []
    width: int | None = None
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        for row in reader:
            if not row:
                continue
            if width is None:
                width = len(row)
            elif len(row) != width:
                return None, f"row {reader.line_num}: expected {width} columns, found {len(row)}"
            rows.append((reader.line_num, row))
    except csv.Error:
        return None, f"row {reader.line_num}: not valid CSV"
    if not rows:
        return None, "file is empty"
    return rows, None


def _file_parses(text: str, fmt: str) -> ItemResult:
    if fmt == "csv":
        rows, refusal = _csv_rows(text)
        if refusal is not None:
            return ItemResult("failing", refusal)
        return ItemResult("passing", f"parses as csv, {len(rows)} rows")
    if fmt == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return ItemResult("failing", f"not valid JSON (line {exc.lineno})")
        return ItemResult("passing", "parses as json")
    if fmt == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return ItemResult("failing", "not valid TOML")
        return ItemResult("passing", "parses as toml")
    if not text.strip():
        return ItemResult("failing", "file is empty")
    return ItemResult("passing", "non-empty markdown")


def _csv_columns(text: str, columns: list[str], min_rows: int) -> ItemResult:
    rows, refusal = _csv_rows(text)
    if refusal is not None:
        return ItemResult("failing", refusal)
    header = [cell.strip() for cell in rows[0][1]]
    index: dict[str, int] = {}
    for column in columns:
        if column not in header:
            return ItemResult("failing", f"missing column {_quote(column)}")
        index[column] = header.index(column)
    data = rows[1:]
    for number, row in data:
        for column in columns:
            if not row[index[column]].strip():
                return ItemResult("failing", f"row {number}: empty {_quote(column)}")
    if len(data) < min_rows:
        return ItemResult("failing", f"{len(data)} data rows, need {min_rows}")
    return ItemResult("passing", f"{len(data)} data rows")


def _quote(name: str) -> str:
    flat = " ".join(name.split())[:60]
    return f"'{flat}'"
