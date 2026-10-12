"""Turning a change into a new config document, and showing it.

`preview` is the whole of it: a change set (ops or a raw document, credentials,
the admin list) applied to the current `config.toml`, refused where it touches
what the editor may not change, validated by `istota apply --check` in a child
process, and described without a credential value in it. The web's preview and
submit both run it; a submit then stores what it returns
(`config_editor.requests.create`).

A path is a list of strings, never a dotted string, since a key can hold a dot.
`ChangeError` is a change set that cannot be read at all; anything a readable
change set gets wrong is one of `Preview.errors` or a blocking problem.

`diff_documents` is the one diff of two config documents, used by
`istota apply`'s printed plan and by the preview. `has_comments` is what the
view warns on, since every write goes through `toml_write`.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from istota.config_editor import secrets
from istota.lib import toml_write


def has_comments(text: str) -> bool:
    """Whether TOML `text` holds a comment, which a rewrite through `toml_write` drops.

    A `#` inside a string is not one, so strings are skipped: basic and literal,
    single- and multi-line, with a basic string's escapes.
    """
    index, size = 0, len(text)
    quote: str | None = None
    while index < size:
        if quote is None:
            if text.startswith('"""', index) or text.startswith("'''", index):
                quote = text[index:index + 3]
                index += 3
                continue
            char = text[index]
            if char == "#":
                return True
            if char in "\"'":
                quote = char
            index += 1
            continue
        if quote[0] == '"' and text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote, index):
            index += len(quote)
            quote = None
            continue
        if len(quote) == 1 and text[index] == "\n":
            quote = None
        index += 1
    return False


def diff_documents(
    current: dict, new: dict, *, fromfile: str = "a/config.toml", tofile: str = "b/config.toml",
) -> list[str]:
    """A unified diff of two documents, as lines without their newlines.

    Both are redacted before they are rendered (`secrets.redact_pair`), rather
    than lines of a rendered diff being redacted by key name, which let a
    header value under `[brain.native.extra_headers]` through. A key the view
    masks rather than redacts (`MASKED_KEYS`) is masked here too, since the
    diff is stored with the request and shown in its history. Both sides go
    through `toml_write`, so a hand-written file's formatting and comments make
    no diff lines.
    """
    current_redacted, new_redacted = secrets.redact_pair(current, new)
    _mask_pair(current, new, current_redacted, new_redacted)
    return [
        line.rstrip("\n")
        for line in difflib.unified_diff(
            toml_write.dumps(current_redacted).splitlines(keepends=True),
            toml_write.dumps(new_redacted).splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    ]


def _mask_pair(current: dict, new: dict, current_shown: dict, new_shown: dict) -> None:
    """The view's masked keys (`MASKED_KEYS`) masked in the two documents a diff
    renders, in place. A changed value whose mask reads the same is marked, so
    it still makes a diff line."""
    from istota.webui.admin_config_view import MASKED_KEYS

    for dotted, mask in MASKED_KEYS.items():
        path = tuple(dotted.split("."))
        found_current, value_current = secrets.lookup(current, path)
        found_new, value_new = secrets.lookup(new, path)
        if found_current:
            _set_at(current_shown, path, mask(value_current))
        if found_new:
            shown = mask(value_new)
            if found_current and value_new != value_current and shown == mask(value_current):
                shown += " (changed)"
            _set_at(new_shown, path, shown)


class ChangeError(ValueError):
    """A change set that cannot be read, so nothing can be previewed.

    The message never repeats a value from the change set.
    """


#: Why nothing is changed from the web while the live file does not parse:
#: every check compares against it, and against nothing a document dropping
#: `db_path` would pass.
UNREADABLE_CONFIG = (
    "config.toml does not parse, so it cannot be changed from here; fix it on "
    "the host with `istota-stack apply`, or copy back a `config.toml.bak.<timestamp>`"
)


_MAX_SEGMENTS = 16
_MAX_SEGMENT_CHARS = 256
_MISSING = object()


def _check_path(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_SEGMENTS:
        raise ChangeError("a path is a non-empty list of key names")
    for segment in raw:
        if not isinstance(segment, str) or not segment or len(segment) > _MAX_SEGMENT_CHARS:
            raise ChangeError("a path is a non-empty list of key names")
    return tuple(raw)


def _check_value(value: Any) -> None:
    """A `set` value: a scalar, or an array of them. A table is `set_toml`'s."""
    if value is None:
        raise ChangeError("a value is null, which TOML has no word for; unset a key for its default")
    if isinstance(value, dict):
        raise ChangeError("a table is set with set_toml, not set")
    if isinstance(value, list):
        for item in value:
            _check_value(item)
        return
    if not isinstance(value, (str, int, float, bool)):
        raise ChangeError("a value is not a string, number, boolean or array")


def _set_at(document: dict, path: tuple[str, ...], value: Any) -> None:
    node = document
    for depth, segment in enumerate(path[:-1]):
        child = node.get(segment, _MISSING)
        if child is _MISSING:
            child = node[segment] = {}
        if not isinstance(child, dict):
            raise ChangeError(f"{secrets.label(path[:depth + 1])} is not a table")
        node = child
    node[path[-1]] = value


def _unset_at(document: dict, path: tuple[str, ...]) -> None:
    """Remove the key at `path`, then every table that removal left empty."""
    trail = [document]
    for segment in path[:-1]:
        child = trail[-1].get(segment)
        if not isinstance(child, dict):
            return
        trail.append(child)
    if path[-1] not in trail[-1]:
        return
    del trail[-1][path[-1]]
    for depth in range(len(path) - 1, 0, -1):
        if trail[depth]:
            break
        del trail[depth - 1][path[depth - 1]]


def _snippet_value(path: tuple[str, ...], snippet: Any) -> Any:
    """What a `set_toml` snippet defines for the last segment of `path`.

    The snippet's one top-level key is that segment, written out, so removing
    the key (unset) and setting it empty (`x = []`) stay two different edits.
    """
    if not isinstance(snippet, str):
        raise ChangeError("set_toml takes TOML text")
    try:
        parsed = tomllib.loads(snippet)
    except tomllib.TOMLDecodeError as exc:
        raise ChangeError(f"the TOML for {secrets.label(path)} does not parse: {exc}") from None
    if list(parsed) != [path[-1]]:
        raise ChangeError(
            f"the TOML for {secrets.label(path)} must define {secrets.label(path[-1:])} "
            "and nothing else"
        )
    return parsed[path[-1]]


def apply_ops(document: dict, ops: Any) -> dict:
    """A copy of `document` with `ops` applied in order."""
    if not isinstance(ops, list):
        raise ChangeError("ops is not a list")
    new = copy.deepcopy(document)
    for op in ops:
        if not isinstance(op, dict):
            raise ChangeError("an op is not an object")
        kind = op.get("op")
        path = _check_path(op.get("path"))
        if kind == "set":
            if "value" not in op:
                raise ChangeError("a set op has no value")
            _check_value(op["value"])
            _set_at(new, path, copy.deepcopy(op["value"]))
        elif kind == "unset":
            _unset_at(new, path)
        elif kind == "set_toml":
            _set_at(new, path, _snippet_value(path, op.get("toml")))
        else:
            raise ChangeError("an op is not set, unset or set_toml")
    return new


def apply_raw(text: Any) -> dict:
    """The whole-file alternative: `text` is the new document."""
    if not isinstance(text, str):
        raise ChangeError("raw is not TOML text")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ChangeError(f"the TOML does not parse: {exc}") from None


def _differs(new: dict, current: dict, path: tuple[str, ...]) -> bool:
    return secrets.lookup(new, path) != secrets.lookup(current, path)


def changed_paths(current: Any, new: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """The deepest paths whose value differs between two documents.

    Tables are walked, an added or removed one too; an array is one value.
    """
    tables = (isinstance(current, dict) or current is _MISSING) and (
        isinstance(new, dict) or new is _MISSING
    )
    if tables and (isinstance(current, dict) or isinstance(new, dict)):
        current = current if isinstance(current, dict) else {}
        new = new if isinstance(new, dict) else {}
        found: list[tuple[str, ...]] = []
        for key in sorted(set(current) | set(new)):
            found.extend(changed_paths(
                current.get(key, _MISSING), new.get(key, _MISSING), prefix + (key,),
            ))
        return found
    if current is _MISSING and new is _MISSING:
        return []
    return [prefix] if current != new else []


def _methods(value: Any) -> list[str]:
    """A multi-choice value's words, read the way `normalize_auth_methods` reads
    `[web] auth`: a comma-separated string or a list."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]


def check_choices(new: dict, current: dict) -> list[str]:
    """A changed key with a closed vocabulary must hold its words."""
    from istota.config import FIELD_CHOICES, MULTI_CHOICES

    errors = []
    for path, choices in FIELD_CHOICES.items():
        found, value = secrets.lookup(new, path)
        if found and _differs(new, current, path) and value not in choices:
            shown = ", ".join(repr(choice) for choice in choices)
            errors.append(f"{secrets.label(path)} must be one of {shown}")
    for path, vocabulary in MULTI_CHOICES.items():
        found, value = secrets.lookup(new, path)
        if not found or not _differs(new, current, path):
            continue
        words = tuple(vocabulary())
        if not isinstance(value, (list, str)) or any(word not in words for word in _methods(value)):
            shown = ", ".join(repr(word) for word in words)
            errors.append(f"{secrets.label(path)} takes a list of {shown}")
    return errors


def check_sign_in(new: dict, current: dict) -> list[str]:
    """A changed `[web] auth` keeps `email`, and is never `none`.

    Every admin signs in with their native identity, so a set without `email`
    locks every admin out; the web refuses to launch on `none` in the container.
    """
    from istota.config import Config

    path = ("web", "auth")
    if not _differs(new, current, path):
        return []
    found, value = secrets.lookup(new, path)
    methods = _methods(value) if found else list(Config().web.auth)
    errors = []
    if "none" in methods:
        errors.append("[web] auth cannot be none: the web UI refuses to start without sign-in")
    if "email" not in methods:
        errors.append(
            "[web] auth must keep email: every admin signs in with their email "
            "identity, so without it nobody could sign in to change it back"
        )
    return errors


_ADMIN_ID = re.compile(r"^[^\s#\x00-\x1f\x7f]{1,128}$")


def check_admins(admins: list[str], *, acting_admin: str, db_path: Path) -> list[str]:
    """The new allowlist: not empty, holding the acting admin, every id with a
    live native identity.

    An empty file means "everyone" to `Config.is_admin` and "nobody" to the
    web's admin check, so it is never written from here. An identity implies a
    profile, so this is the profile check too.
    """
    from istota.webui import auth as web_auth

    if not admins:
        return ["the admin list cannot be empty"]
    errors = []
    if acting_admin not in admins:
        errors.append("the admin list must keep you; another admin can remove you")
    for user_id in admins:
        identity = web_auth.get_identity(db_path, user_id)
        if identity is None or identity.disabled:
            errors.append(
                f"{user_id} has no live sign-in identity; give them one on the Users page first"
            )
    return errors


def check_read_only(
    new: dict, current: dict, environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Keys the editor shows and never edits, and keys the environment sets.

    `[users.*]` is the exception: seed-only, it may be edited in the raw editor.
    """
    from istota.config_editor import schema

    errors = []
    for path, reason in schema.READ_ONLY.items():
        if path == ("users",):
            continue
        if _differs(new, current, path):
            errors.append(f"{secrets.label(path)} is not edited here. {reason}")
    for path in changed_paths(current, new):
        variable = schema.env_override_for(path, environ)
        if variable:
            errors.append(
                f"{secrets.label(path)} is set by {variable}, which wins over config.toml"
            )
    return errors


def check_file_credentials(new: dict, current: dict) -> list[str]:
    """A credential with a stack secrets file is never given a value in the file.

    The secrets file wins at load, so a value written inline would do nothing
    whenever the file is set. Removing a stale inline value is allowed.
    """
    from istota.config_editor import schema

    errors = []
    for path, name in schema.file_credentials().items():
        found, _ = secrets.lookup(new, path)
        if found and _differs(new, current, path):
            errors.append(
                f"{secrets.label(path)} is the credential {name}; set it as a "
                "credential rather than in config.toml"
            )
    return errors


def check_dates(new: dict) -> list[str]:
    """A TOML date or time anywhere in the new document, which the host's
    plan cannot carry: `plan.json` is JSON. Refused here so such a change never
    reaches the host, where `take` would refuse it anyway."""
    import datetime

    errors: list[str] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (str(index),))
        elif isinstance(value, (datetime.date, datetime.time)):
            errors.append(
                f"{secrets.label(path)} holds a date or time value, which the host "
                "applier cannot carry; change it on the host with `istota-stack apply`"
            )

    walk(new, ())
    return errors


def restore_change_set(document_toml: str, current_text: str, base_digest: str) -> tuple[dict, list[dict]]:
    """A change set restoring a history entry's document, and what restoring it drops.

    The stored document holds a sentinel at every secret leaf, so credentials
    keep their current values. A sentinel whose credential has since been
    cleared is dropped, and the credential stays cleared, each named in a
    non-blocking warning. Credentials and the admin list are never restored.
    """
    try:
        stored = tomllib.loads(document_toml)
    except tomllib.TOMLDecodeError as exc:
        raise ChangeError(f"the stored document does not parse: {exc}") from None
    try:
        current = tomllib.loads(current_text)
    except tomllib.TOMLDecodeError:
        current = {}
    document, dropped = secrets.drop_missing_sentinels(stored, current)
    try:
        raw = toml_write.dumps(document)
    except toml_write.TomlWriteError as exc:
        raise ChangeError(f"the stored document cannot be written: {exc}") from None
    warnings = [
        {
            "kind": "warning",
            "path": list(secrets.parse_label(name) or ()),
            "message": f"{name} was cleared after this change; it stays cleared",
            "blocking": False,
            "pre_existing": False,
        }
        for name in dropped
    ]
    return {"base_digest": base_digest, "ops": [], "raw": raw}, warnings


def _read_secrets(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    """The change set's credentials, split into file and inline ones."""
    from istota.config_editor import schema
    from istota.setup_wizard import SECRET_NAMES

    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        raise ChangeError("secrets is not an object")
    file_paths = set(schema.file_credentials())
    files: dict[str, str] = {}
    inline: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ChangeError(
                'a credential value is not a string; "" clears one, and an '
                "omitted name is unchanged"
            )
        if name in SECRET_NAMES:
            files[name] = value
            continue
        path = secrets.parse_label(name)
        if path is None or path in file_paths or not secrets.is_secret_path(path):
            raise ChangeError("secrets names something that is not a credential")
        inline[name] = value
    return files, inline


def _read_admins(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ChangeError("admins is not a list of user ids")
    admins: list[str] = []
    for item in raw:
        user_id = item.strip()
        if not _ADMIN_ID.match(user_id):
            raise ChangeError("an entry in admins is not a user id")
        if user_id not in admins:
            admins.append(user_id)
    return admins


def file_credential_problems(new: dict, environ: Mapping[str, str]) -> list[dict]:
    """A file credential also set in `config.toml`, where the set file wins."""
    from istota.config_editor import schema

    found = []
    for path, name in schema.file_credentials().items():
        present, _ = secrets.lookup(new, path)
        if present and environ.get(name.upper(), "").strip():
            found.append({
                "kind": "warning",
                "path": list(path),
                "message": (
                    f"{secrets.label(path)} is also set in config.toml, where it is "
                    f"ignored: the credential {name} wins"
                ),
                "blocking": False,
                "pre_existing": False,
                "action": "unset",
            })
    return found


def section_of(path: tuple[str, ...]) -> str:
    """The first one or two segments, when they are field names on the Config
    tree; a log line never carries a segment somebody typed."""
    from istota.config import Config

    names = {item.name for item in dataclasses.fields(Config)}
    if not path or path[0] not in names:
        return "(unknown)"
    owner = getattr(Config(), path[0], None)
    if len(path) > 2 and dataclasses.is_dataclass(owner):
        if path[1] in {item.name for item in dataclasses.fields(owner)}:
            if dataclasses.is_dataclass(getattr(owner, path[1], None)):
                return f"{path[0]}.{path[1]}"
    return path[0]


def loggable_secret_name(name: str) -> str:
    """A credential's name for a log line or a notification.

    A file credential's name is one of `SECRET_NAMES`. An inline one is named by
    its label, whose segments can be anything an admin typed (a header name), so
    it is shown as its section instead.
    """
    from istota.setup_wizard import SECRET_NAMES

    if name in SECRET_NAMES:
        return name
    path = secrets.parse_label(name)
    return f"{section_of(path) if path else '(unknown)'} (inline)"


def _related(a: tuple, b: tuple) -> bool:
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def running_tasks(db_path: Path | None) -> int:
    """How many tasks are running now: an applied change restarts the stack."""
    from istota import db

    if not db.database_present(db_path):
        return 0
    with db.get_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]


@dataclass
class Preview:
    """What a change set would do.

    `document` holds a sentinel at every secret leaf. The only credential
    values are in `file_secrets` and `inline_secrets`, which `as_json` leaves
    out.
    """

    document: dict
    inline_secrets: dict[str, str] = field(default_factory=dict)
    file_secrets: dict[str, str] = field(default_factory=dict)
    admins: list[str] | None = None
    diff: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    changed: bool = False
    changed_sections: list[str] = field(default_factory=list)
    changed_keys: int = 0
    risky_keys: list[str] = field(default_factory=list)
    secret_names: list[str] = field(default_factory=list)
    admins_changed: bool = False
    running_tasks: int = 0

    @property
    def blocking(self) -> list[dict]:
        return [problem for problem in self.problems if problem["blocking"]]

    @property
    def warnings(self) -> list[dict]:
        return [problem for problem in self.problems if not problem["blocking"]]

    @property
    def blocked(self) -> bool:
        return bool(self.errors or self.blocking)

    @property
    def secrets(self) -> dict[str, str]:
        """Every new credential value, by file name or inline label, for sealing."""
        return {**self.file_secrets, **self.inline_secrets}

    @property
    def document_toml(self) -> str:
        return toml_write.dumps(self.document)

    def as_json(self) -> dict:
        return {
            "diff": self.diff,
            "problems": {"blocking": self.blocking, "warnings": self.warnings},
            "errors": self.errors,
            "changed": self.changed,
            "changed_sections": self.changed_sections,
            "changed_keys": self.changed_keys,
            "risky_keys": self.risky_keys,
            "secret_names": self.secret_names,
            "admins_changed": self.admins_changed,
            "running_tasks": self.running_tasks,
        }


async def preview(
    current_text: str,
    change_set: Any,
    *,
    config: Any,
    acting_admin: str,
    run_check: Callable[[dict], Awaitable[Any]],
    environ: Mapping[str, str] | None = None,
) -> Preview:
    """What `change_set` would make of `current_text`, the live `config.toml`.

    `run_check` validates the new document in a child process
    (`config_editor.check.run_check`); a unit test passes a fake. Raises
    `ChangeError` for a change set that cannot be read. The base digest, the
    step-up, the confirmations and managed resources are the route's to check.
    """
    from istota.config_editor import schema
    from istota.config_editor.check import problems_of

    environ = os.environ if environ is None else environ
    if not isinstance(change_set, dict):
        raise ChangeError("the change set is not an object")
    try:
        base = tomllib.loads(current_text)
    except tomllib.TOMLDecodeError:
        raise ChangeError(UNREADABLE_CONFIG) from None
    ops = change_set.get("ops") or []
    raw = change_set.get("raw")
    if raw is not None:
        if ops:
            raise ChangeError("raw replaces the whole file, so ops must be empty")
        candidate = apply_raw(raw)
    else:
        candidate = apply_ops(base, ops)
    try:
        new = secrets.restore_sentinels(candidate, base)
    except secrets.SentinelError as exc:
        raise ChangeError(str(exc)) from None
    file_secrets, inline_given = _read_secrets(change_set.get("secrets"))
    for name, value in inline_given.items():
        path = secrets.parse_label(name)
        if value == "":
            _unset_at(new, path)
        else:
            _set_at(new, path, value)
    admins = _read_admins(change_set.get("admins"))

    paths_by_name = {name: path for path, name in schema.file_credentials().items()}
    errors: list[str] = []
    errors += check_read_only(new, base, environ)
    errors += check_file_credentials(new, base)
    for name in sorted(file_secrets):
        path = paths_by_name.get(name)
        if path in schema.READ_ONLY:
            errors.append(f"the credential {name} is not changed here. {schema.READ_ONLY[path]}")
    errors += check_choices(new, base)
    errors += check_sign_in(new, base)
    errors += check_dates(new)
    current_admins = sorted(getattr(config, "admin_users", None) or ())
    admins_changed = admins is not None and sorted(admins) != current_admins
    if admins_changed:
        errors += check_admins(admins, acting_admin=acting_admin, db_path=config.db_path)

    try:
        diff = diff_documents(base, new)
    except toml_write.TomlWriteError as exc:
        raise ChangeError(f"the new config.toml cannot be written: {exc}") from None
    redacted, inline_new = secrets.extract_inline_secrets(new, base)
    changed = changed_paths(base, new)
    removed = [
        secrets.label(path) for path in changed
        if secrets.is_secret_path(path)
        and not secrets.lookup(new, path)[0]
        and path not in paths_by_name.values()
    ]

    problems = problems_of(await run_check(new))
    for problem in problems:
        path = tuple(problem.get("path") or ())
        problem["pre_existing"] = bool(path) and not any(_related(path, item) for item in changed)
    problems += file_credential_problems(new, environ)

    risky = {secrets.label(path) for path in schema.RISKY_KEYS if _differs(new, base, path)}
    for name in file_secrets:
        path = paths_by_name.get(name)
        if path in schema.RISKY_KEYS:
            risky.add(secrets.label(path))
    if admins_changed:
        risky.add("admins")

    return Preview(
        document=redacted,
        inline_secrets=inline_new,
        file_secrets=file_secrets,
        admins=admins if admins_changed else None,
        diff=diff,
        problems=problems,
        errors=errors,
        changed=bool(changed or file_secrets or admins_changed),
        changed_sections=sorted({section_of(path) for path in changed}),
        changed_keys=len(changed),
        risky_keys=sorted(risky),
        secret_names=sorted(set(file_secrets) | set(inline_new) | set(removed)),
        admins_changed=admins_changed,
        running_tasks=running_tasks(getattr(config, "db_path", None)),
    )
