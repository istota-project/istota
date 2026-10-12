"""Secret leaves of a config document: finding, redacting, restoring, extracting.

Every secret leaf is found by one path-aware predicate, `is_secret_path`, which
is the admin view's (`webui.admin_config_view`), not a second copy: a path is
secret when a field on it is credential-named, or it is at or under an
always-secret field such as `[brain.native.extra_headers]`, whatever a header
there is called. Redaction, the raw editor's sentinels, stored requests and
diffs all decide by it.

A document here is what `tomllib` returns: tables are dicts, arrays are lists.
Nothing in this module mutates its input.
"""

from __future__ import annotations

import re
from typing import Any

from istota.lib import toml_write
from istota.webui.admin_config_view import is_secret_path

__all__ = [
    "SENTINEL", "SentinelError", "drop_missing_sentinels", "extract_inline_secrets",
    "is_secret_path", "label", "lookup", "parse_label", "redact_document",
    "restore_sentinels",
]

#: What a secret leaf reads as in a document shown to an admin or stored in a
#: request. On the way back in it means "keep the value stored now".
SENTINEL = "<unchanged>"

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


class SentinelError(ValueError):
    """A sentinel stands where the current document has no value to keep."""


def label(path: tuple | list) -> str:
    """`path` as a TOML dotted key, a segment holding a dot or a space quoted.

    The one form a path is shown in and a credential is named by in a change
    set's `secrets`, so `a."b.c"` and `a.b.c` stay distinct.
    """
    parts = []
    for segment in path:
        text = str(segment)
        parts.append(text if _BARE.match(text) else toml_write.toml_string(text))
    return ".".join(parts)


def parse_label(text: str) -> tuple[str, ...] | None:
    """The path `label` wrote as `text`, or None for anything `label` would not write.

    Read by parsing `text` as a TOML key and writing it back: only text that
    comes back identical names a path, so nothing beside the key gets through.
    """
    import tomllib

    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = tomllib.loads(f"{text} = 0")
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    path: list[str] = []
    node: Any = parsed
    while isinstance(node, dict) and len(node) == 1:
        (key, node), = node.items()
        path.append(key)
    if node != 0 or isinstance(node, bool) or not path or label(path) != text:
        return None
    return tuple(path)


def _walk(value: Any, path: tuple, leaf):
    """A copy of `value` with `leaf(path, value)` in place of every secret leaf.

    A table or array at a secret path is walked rather than replaced whole, so
    the header names under `[brain.native.extra_headers]` stay visible and each
    value is its own leaf.
    """
    if isinstance(value, dict):
        return {key: _walk(item, path + (key,), leaf) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, path + (str(index),), leaf) for index, item in enumerate(value)]
    if path and is_secret_path(path):
        return leaf(path, value)
    return value


def redact_document(document: dict, *, placeholder: str = SENTINEL) -> dict:
    """`document` with every secret leaf replaced by `placeholder`."""
    return _walk(document, (), lambda path, value: placeholder)


#: The two placeholders of a diff: a secret leaf as it is, and one this change
#: sets to a different value. Neither is `SENTINEL`, which means "keep".
REDACTED = "<redacted>"
CHANGED = "<changed>"


def redact_pair(current: dict, new: dict) -> tuple[dict, dict]:
    """Both documents redacted for a diff between them.

    Every secret leaf of `current` reads `REDACTED`. In `new`, a leaf holding
    the same value as `current` at the same path reads `REDACTED` too, so it
    makes no diff line, and any other reads `CHANGED`, so a changed or added
    credential shows as one without its value.
    """

    def mark(path: tuple, value: Any) -> str:
        found, stored = lookup(current, path)
        return REDACTED if found and stored == value else CHANGED

    return redact_document(current, placeholder=REDACTED), _walk(new, (), mark)


def lookup(document: Any, path: tuple) -> tuple[bool, Any]:
    """Whether `path` is in `document`, and the value there; an index segment
    steps into a list."""
    node = document
    for segment in path:
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return False, None
    return True, node


def restore_sentinels(new: dict, current: dict, values: dict[str, Any] | None = None) -> dict:
    """`new` with the current value put back wherever a sentinel stands.

    `values` are new credential values by `label`, as `extract_inline_secrets`
    took them out of a document; one there wins over the current value. A
    sentinel with neither is refused with `SentinelError`: there is no stored
    value to keep.
    """
    values = values or {}

    def restore(path: tuple, value: Any) -> Any:
        if value != SENTINEL:
            return value
        if label(path) in values:
            return values[label(path)]
        found, stored = lookup(current, path)
        if not found:
            raise SentinelError(f"no stored value to keep at {label(path)}")
        return stored

    return _restore_everywhere(new, (), restore)


def drop_missing_sentinels(document: dict, current: dict) -> tuple[dict, list[str]]:
    """`document` without the sentinels `current` has no value for, and their labels.

    For restoring a history entry: a credential cleared since the entry was
    applied stays cleared, rather than refusing the whole restore the way
    `restore_sentinels` refuses a sentinel with nothing to keep. An array
    element is dropped with its slot.
    """
    dropped: list[str] = []

    def keep(value: Any, path: tuple) -> bool:
        if value != SENTINEL or lookup(current, path)[0]:
            return True
        dropped.append(label(path))
        return False

    def walk(value: Any, path: tuple) -> Any:
        if isinstance(value, dict):
            return {
                key: walk(item, path + (key,))
                for key, item in value.items()
                if keep(item, path + (key,))
            }
        if isinstance(value, list):
            return [
                walk(item, path + (str(index),))
                for index, item in enumerate(value)
                if keep(item, path + (str(index),))
            ]
        return value

    return walk(document, ()), dropped


def _restore_everywhere(value: Any, path: tuple, restore):
    # Wherever the sentinel appears, not only at a secret path: a sentinel at a
    # path the predicate does not cover is still a value the admin never saw.
    if isinstance(value, dict):
        return {key: _restore_everywhere(item, path + (key,), restore) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_everywhere(item, path + (str(index),), restore) for index, item in enumerate(value)]
    return restore(path, value)


def extract_inline_secrets(
    document: dict, current: dict | None = None,
) -> tuple[dict, dict[str, Any]]:
    """Move every secret leaf that is not a sentinel out of `document`.

    Returns the document with a sentinel at each of those leaves, and the
    values keyed by `label(path)`. A value an admin typed into a set, a table
    snippet or the raw editor is a new credential, and is stored sealed rather
    than in the document. With `current`, a leaf holding the value `current`
    holds at the same path is not returned: the sentinel keeps it.
    """
    found: dict[str, Any] = {}

    def take(path: tuple, value: Any) -> str:
        if value == SENTINEL:
            return SENTINEL
        if current is not None:
            exists, stored = lookup(current, path)
            if exists and stored == value:
                return SENTINEL
        found[label(path)] = value
        return SENTINEL

    return _walk(document, (), take), found
