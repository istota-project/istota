"""The tree's one TOML writer: `toml_string` and `dumps`.

`tomllib` reads and does not write. Three writers grew beside it: the standalone
wizard's `_toml_str` with hand-assembled lines, the container wizard's
`tomli_w.dumps`, and the testbed's own `toml_dumps`, because the testbed may not
depend on what istota depends on. This is all three, and `istota apply` renders
through it as well.

`dumps` takes what `tomllib` returns (tables, arrays of tables, strings,
integers, finite or infinite floats, booleans, dates and times, arrays) and
writes text that `tomllib.loads` reads back as the same document. Scalars of a
table come before its sub-tables, so key order within a table may move; nothing
reading a config depends on it. `None` has no TOML form and is refused rather
than dropped, since a plan that says null for a key means something the file
cannot say.

stdlib-only leaf, imports nothing from the package: the testbed imports it.
"""

from __future__ import annotations

import datetime as _dt
import math
import re

__all__ = ["TomlWriteError", "dumps", "toml_string"]

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class TomlWriteError(ValueError):
    """A value with no TOML form."""


def toml_string(value: str) -> str:
    """TOML basic-string escaping: backslash, double quote, control characters.

    The control-character arm is not decoration. TOML 1.0 forbids raw U+0000 to
    U+0008, U+000A to U+001F and U+007F in a basic string, and a pasted
    credential is the realistic carrier: a line-oriented read cannot deliver a
    newline but happily delivers an ESC or a DEL, and a file holding one will
    not parse. Tab is left alone, since TOML permits it raw.
    """
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif (char < " " and char != "\t") or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise TomlWriteError(f"a TOML key must be a string, got {type(key).__name__}")
    return key if _BARE_KEY.match(key) else toml_string(key)


def _path(parts: tuple[str, ...]) -> str:
    return ".".join(_key(part) for part in parts)


def _float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _value(value: object, where: str) -> str:
    if value is None:
        raise TomlWriteError(f"{where} is null, which TOML cannot write")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item, f"{where}[]") for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{_key(key)} = {_value(item, f'{where}.{key}')}" for key, item in value.items()
        )
        return "{ " + inner + " }" if inner else "{}"
    raise TomlWriteError(f"{where} is a {type(value).__name__}, which TOML cannot write")


def _is_table_array(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, dict) for item in value)
    )


def _split(table: dict) -> tuple[list, list, list]:
    scalars, tables, arrays = [], [], []
    for key, value in table.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif _is_table_array(value):
            arrays.append((key, value))
        else:
            scalars.append((key, value))
    return scalars, tables, arrays


def dumps(document: dict) -> str:
    """A TOML document from nested dicts, as `tomllib.loads` would read it back."""
    if not isinstance(document, dict):
        raise TomlWriteError(f"a TOML document is a table, got {type(document).__name__}")
    lines: list[str] = []

    def body(table: dict, path: tuple[str, ...], header: str | None) -> None:
        scalars, tables, arrays = _split(table)
        # A table holding only sub-tables needs no header of its own; an empty
        # one does, or it would not exist after a round trip.
        if header is not None and (scalars or not (tables or arrays) or header.startswith("[[")):
            lines.append(header)
        for key, value in scalars:
            where = _path((*path, key)) if path else _key(key)
            lines.append(f"{_key(key)} = {_value(value, where)}")
        if scalars or (header is not None and header.startswith("[[")):
            lines.append("")
        for key, value in tables:
            sub = (*path, key)
            body(value, sub, f"[{_path(sub)}]")
        for key, items in arrays:
            sub = (*path, key)
            for item in items:
                body(item, sub, f"[[{_path(sub)}]]")

    body(document, (), None)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
