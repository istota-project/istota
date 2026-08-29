"""Map a parsed TOML document onto the ``Config`` dataclass tree.

``load_config`` used to state the schema a second time: 1159 lines of
``if "x" in data: config.x = data["x"]``, one hand-written line per key, for a
shape the dataclasses in :mod:`istota.config` already declare in full. The
duplication was not free. Three defects live in this file's history and every
one of them is a thing a second copy of a schema does rather than a thing
anybody decided:

**A field the loader never read.** Eleven settings were declared on a
dataclass, documented in ``config/config.example.toml`` and written by the
Ansible template or the Docker render, and simply had no line in the loader —
so the operator set the value, every surface agreed it existed, and the daemon
ran the hardcoded default. ``security.sandbox_ro_paths`` was found this way
before; ``scheduler.max_subtasks_per_task``, a cap on prompt-injection blast
radius, was found the same way while writing this module. A walk over
``dataclasses.fields`` cannot have this bug, because there is no per-key line
to leave out.

**Two defaults for one field.** The dataclass said one thing and the
``.get(key, default)`` in the loader said another, so the value depended on
whether the *section header* was present: a bare ``[sleep_cycle]`` with no keys
under it turned the nightly memory extraction off, because the dataclass
default was ``True`` and the loader's was ``False``. Here the dataclass default
is the only default. Every shipped generator writes these keys explicitly, so
no generated deployment ever depended on the divergence.

**A typo that did nothing.** The old loader ignored unknown keys deliberately
(forward compatibility), but it could not tell an unknown key from a misspelled
one, so ``[breifings]`` and ``max_subtask_dept`` were silently discarded. The
walk knows the whole schema, so it reports what it did not recognise. It still
only warns: refusing to boot on an unknown key would turn a forward-compatible
config into a hard failure on rollback.

What this module deliberately does **not** own is judgement. Anything that
validates, migrates a legacy key, reads two fields to decide one, or builds
something that is not a plain field stays hand-written in
:mod:`istota.config` and is registered here as a hook against its dotted key.
The split is the point: mechanical mapping is generated, decisions are written
down and testable on their own.

Nothing here raises. ``load_config`` runs in the daemon, the web app, the
webhook receiver, every CLI invocation, and every host-side skill CLI
subprocess the skill proxy spawns per call. A malformed value warns and leaves
the dataclass default standing, which is the same failure posture the
hand-written loader had.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from pathlib import Path
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin

logger = logging.getLogger("istota.config")

# A hook receives the raw TOML value and the dotted key, and returns the value
# to set. Returning ``_KEEP`` leaves the dataclass default in place — a hook
# that rejects its input says so that way rather than by raising.
Hook = Callable[[Any, str], Any]

_KEEP = object()
"""Sentinel: leave the field at its dataclass default."""

# Strings accepted for a bool-declared field. TOML has real booleans, so a
# string only arrives when an operator quoted one or a generator emitted an
# unquoted-looking value into a quoted slot. ``bool("false")`` is ``True``,
# which is why this is a table and not a cast: the security block already
# carried a hand-written copy of it for the one key where getting it backwards
# would have left a delete path running.
_TRUE_STRINGS = frozenset({"true", "yes", "on", "1"})
_FALSE_STRINGS = frozenset({"false", "no", "off", "0"})


def _warn(key: str, raw: Any, expected: str) -> Any:
    """Report a value that does not fit its declared type, and keep the default.

    The value is echoed because for a wrong type that is the useful half of the
    message -- except where the field is a credential, which is the one case
    where a type complaint would put a live secret into the rotating app log
    the admin Logs pane reads. A password mistyped as a list is exactly the
    shape that reaches here. ``is_secret_name`` is the codebase's one answer to
    "does this field name hold a credential", so it is reused rather than
    restated; the import is deferred because this module is a leaf on the
    config-load hot path and a warning is the rare case.
    """
    from .admin_config_view import is_secret_name

    shown = "<redacted>" if is_secret_name(key.rsplit(".", 1)[-1]) else repr(raw)
    logger.warning(
        "[config] %s=%s is not %s; ignoring the value and keeping the default",
        key, shown, expected,
    )
    return _KEEP


def coerce_bool(raw: Any, key: str) -> Any:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return _warn(key, raw, "a boolean")


def coerce_int(raw: Any, key: str) -> Any:
    # bool is a subclass of int, and ``int(True)`` is 1. A boolean written where
    # a count belongs is a mistake worth reporting, not one worth silently
    # turning into 1.
    if isinstance(raw, bool):
        return _warn(key, raw, "an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if math.isfinite(raw) and raw.is_integer():
            return int(raw)
        return _warn(key, raw, "an integer")
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return _warn(key, raw, "an integer")
    return _warn(key, raw, "an integer")


def coerce_float(raw: Any, key: str) -> Any:
    if isinstance(raw, bool):
        return _warn(key, raw, "a number")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return _warn(key, raw, "a number")
    else:
        return _warn(key, raw, "a number")
    # NaN compares false against every threshold, so a non-finite value does not
    # fail loudly downstream — it quietly switches off whichever comparison it
    # feeds. The sandbox cache ceiling was hardened against exactly this.
    if not math.isfinite(value):
        return _warn(key, raw, "a finite number")
    return value


def coerce_str(raw: Any, key: str) -> Any:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return _warn(key, raw, "a string")


def coerce_path(raw: Any, key: str) -> Any:
    if isinstance(raw, Path):
        return raw
    if isinstance(raw, str):
        return Path(raw)
    return _warn(key, raw, "a path")


def coerce_optional_path(raw: Any, key: str) -> Any:
    # An explicit empty string clears the field rather than becoming ``Path("")``,
    # which is ``.`` and would read as the current working directory.
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return coerce_path(raw, key)


def coerce_optional_bool(raw: Any, key: str) -> Any:
    if raw is None:
        return None
    return coerce_bool(raw, key)


def _coerce_sequence(raw: Any, key: str, item: Callable[[Any, str], Any], label: str) -> Any:
    if not isinstance(raw, (list, tuple)):
        return _warn(key, raw, label)
    out = []
    for index, element in enumerate(raw):
        value = item(element, f"{key}[{index}]")
        if value is _KEEP:
            # One bad element drops itself rather than taking the whole list
            # with it. That is fail-closed for the lists that grant something
            # (`extra_hosts`, `passthrough_env_vars`, `sandbox_ro_paths`) --
            # a typo in the third entry should not restore a default allowlist
            # wider than the operator wrote.
            #
            # It is fail-open for the two that deny something
            # (`web_fetch.block_hosts`, `extra_blocked_cidrs`), where a dropped
            # element silently widens what the SSRF guard permits. Stated
            # rather than fixed: the alternative is discarding a whole
            # blocklist over one bad entry, which is worse, and the element is
            # named in the warning either way.
            continue
        out.append(value)
    return out


def coerce_str_list(raw: Any, key: str) -> Any:
    return _coerce_sequence(raw, key, coerce_str, "a list of strings")


def coerce_int_list(raw: Any, key: str) -> Any:
    return _coerce_sequence(raw, key, coerce_int, "a list of integers")


def coerce_str_set(raw: Any, key: str) -> Any:
    value = _coerce_sequence(raw, key, coerce_str, "a list of strings")
    return value if value is _KEEP else set(value)


def coerce_dict(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw
    return _warn(key, raw, "a table")


# Declared scalar type -> coercion. Container and optional forms are resolved
# structurally by :func:`_coercion_for` rather than being spelled out here.
SCALARS: dict[Any, Callable[[Any, str], Any]] = {
    bool: coerce_bool,
    int: coerce_int,
    float: coerce_float,
    str: coerce_str,
    Path: coerce_path,
}

_SEQUENCES: dict[Any, Callable[[Callable[[Any, str], Any]], Callable[[Any, str], Any]]] = {
    list: lambda item: lambda raw, key: _coerce_sequence(raw, key, item, "a list"),
    tuple: lambda item: lambda raw, key: _coerce_sequence(raw, key, item, "a list"),
    set: lambda item: lambda raw, key: (
        lambda v: v if v is _KEEP else set(v)
    )(_coerce_sequence(raw, key, item, "a list")),
    frozenset: lambda item: lambda raw, key: (
        lambda v: v if v is _KEEP else frozenset(v)
    )(_coerce_sequence(raw, key, item, "a list")),
}


def _coercion_for(field: dataclasses.Field) -> Callable[[Any, str], Any] | None:
    """The coercion for a field's declared type, or ``None`` if unmapped.

    Resolved **structurally**, through ``typing.get_origin``, rather than by
    matching the annotation's spelling. An earlier version of this was a
    literal table -- and it needed three separate entries just for ``dict``
    (``dict``, ``dict[str, str]``, ``dict[str, str | dict]``), which is the
    tell. A contributor adding a field annotated ``str | None`` or
    ``list[float]`` would have found it silently ignored with only a log line:
    byte-identical to the "field the loader never read" defect this module
    exists to make impossible, reintroduced one annotation at a time.

    ``tests/test_config_mapper.py`` walks the real tree and requires this to
    answer for every declared field, so an unmapped annotation fails the suite
    rather than shipping dead.
    """
    declared = field.type
    # `config.py` does not use `from __future__ import annotations`, so this is
    # a real type object. A string would mean a module that does, and there is
    # nothing sound to resolve it against here, so it stays unmapped.
    if isinstance(declared, str):
        return None
    return _resolve(declared)


def _resolve(declared: Any) -> Callable[[Any, str], Any] | None:
    if declared in SCALARS:
        return SCALARS[declared]

    origin = get_origin(declared)
    if origin is None:
        # A bare `dict` has no origin and no args -- pass the table through.
        return coerce_dict if declared is dict else None

    if origin is dict:
        # Keys and values are left alone: the two mappings in the tree are a
        # verbatim alias table and a per-model override blob, both of which are
        # read by code that knows their shape better than this does.
        return coerce_dict

    if origin in (Union, UnionType):
        args = [a for a in get_args(declared) if a is not type(None)]
        if len(args) != 1:
            return None
        inner = _resolve(args[0])
        if inner is None:
            return None
        if args[0] is Path:
            # An explicit empty string clears the field rather than becoming
            # `Path("")`, which is `.` and would read as the working directory.
            return coerce_optional_path
        return lambda raw, key: None if raw is None else inner(raw, key)

    if origin in _SEQUENCES:
        args = get_args(declared)
        # `tuple[str, ...]` is the variadic spelling and means the same thing
        # here as `list[str]`. A fixed-length `tuple[str, int]` is not a
        # config shape and stays unmapped.
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            args = args[:1]
        if len(args) != 1:
            return None
        item = _resolve(args[0])
        if item is None:
            return None
        return _SEQUENCES[origin](item)

    return None


def _nested_dataclass(instance: Any, field: dataclasses.Field) -> Any:
    """The nested dataclass currently held by ``field``, or ``None``."""
    value = getattr(instance, field.name, None)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value
    return None


def apply_section(
    instance: Any,
    data: dict,
    *,
    prefix: str = "",
    hooks: dict[str, Hook] | None = None,
    unknown: list[str] | None = None,
    skip: frozenset[str] = frozenset(),
    reject: frozenset[str] = frozenset(),
) -> None:
    """Set every field of ``instance`` that ``data`` names, in place.

    ``prefix`` is the dotted path of ``instance`` itself, so hooks and warnings
    address a field the same way the admin config view and the docs do.
    ``skip`` names dotted keys the caller handles itself; they are neither
    mapped nor reported as unknown.

    A nested dataclass recurses into the sub-table of the same name. It is
    mutated in place rather than reconstructed, so a field the caller set before
    calling — or one only a hook knows how to build — survives.
    """
    hooks = hooks or {}
    if not isinstance(data, dict):
        logger.warning(
            "[config] [%s] must be a table, got %s; ignoring the section",
            prefix or "general", type(data).__name__,
        )
        return

    fields = {f.name: f for f in dataclasses.fields(instance)}
    handled: set[str] = set()

    for name, field in fields.items():
        key = f"{prefix}.{name}" if prefix else name
        if key in skip:
            handled.add(name)
            continue

        # A declared field that is nonetheless not a *setting* -- the loader
        # owns it, or it is a test seam. It must not be settable from the file,
        # and it must not pass silently either: an operator who writes one has
        # made a mistake nothing else would ever tell them about, because being
        # a real field is exactly what keeps it out of the unknown-key report.
        if key in reject:
            handled.add(name)
            if name in data and unknown is not None:
                unknown.append(key)
            continue

        hook = hooks.get(key)

        # A hook is consulted before the recursion, so one may stand in for a
        # whole nested section -- `[developer.container]` is normalised from
        # three historical spellings and has to be built rather than walked.
        nested = _nested_dataclass(instance, field)
        if nested is not None and hook is None:
            handled.add(name)
            if name in data:
                apply_section(
                    nested, data[name], prefix=key, hooks=hooks,
                    unknown=unknown, skip=skip, reject=reject,
                )
            continue

        if name not in data:
            continue
        handled.add(name)
        raw = data[name]

        if hook is not None:
            value = hook(raw, key)
        else:
            coerce = _coercion_for(field)
            if coerce is None:
                # An annotation the table does not know. Refusing is the safe
                # direction: the field keeps its default and the operator is
                # told, rather than the walk assigning a raw TOML value to a
                # field whose type nobody checked.
                logger.warning(
                    "[config] %s has unmapped type %r; ignoring the value",
                    key, str(field.type),
                )
                continue
            value = coerce(raw, key)

        if value is not _KEEP:
            setattr(instance, name, value)

    if unknown is not None:
        for name in data:
            if name in handled or name in fields:
                continue
            key = f"{prefix}.{name}" if prefix else name
            if key in skip:
                continue
            unknown.append(key)


def report_unknown(unknown: list[str], config_path: Any = None) -> None:
    """Warn once about every key the schema does not declare.

    One line rather than one per key: an operator upgrading past a removed
    setting should get a list they can act on, not a wall that buries the rest
    of the boot log. Unknown keys are tolerated on purpose — a config written
    for a newer version has to load on an older one for a rollback to work —
    so this never refuses to boot.
    """
    if not unknown:
        return
    logger.warning(
        "[config] %d unrecognised key(s) in %s and ignored: %s. "
        "A removed setting is expected here; anything else is a typo.",
        len(unknown), config_path or "the config file", ", ".join(sorted(unknown)),
    )
