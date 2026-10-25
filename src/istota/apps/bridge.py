"""The server half of the bridge: every data call an app makes is checked and answered here.

The shell relays a frame's message to ``POST .../v/{version}/call``;
:func:`dispatch` then, in order:

1. refuses ``stale_version`` unless ``version`` is the app's current version;
2. refuses ``not_granted`` unless that version holds the capability, by the
   capability's full text as stored in ``app_grants``;
3. refuses ``rate_limited`` past ``[apps] calls_per_minute`` in the last 60
   seconds, counting the app's calls across all its versions;
4. runs the handler for ``(capability family, op)`` host-side as the user,
   with the grant's stored scope;
5. records the call in ``app_calls`` with its status and a digest of its
   arguments, pruning calls past ``calls_retention_days``, and commits.

Handlers check their own arguments against the scope. ``kv:own`` reaches
namespace ``app.<id>`` in the framework database's ``istota_kv``, values
stored as JSON text so the ``kv`` skill reads them back unchanged. ``files:own``
reaches ``{bot_dir}/apps/<id>/data/`` in the user's workspace, every component
walked by descriptor with no link followed (``sandbox/confined.py``).

A handler raises :class:`BridgeError` with one of the kit's codes; anything
else is logged by app, version, capability and op only and answered
``internal``. Arguments are never logged or stored.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from istota import db as framework_db
from istota.apps import db as apps_db
from istota.apps.capabilities import split_capability
from istota.apps.install import DATA, kv_namespace
from istota.lib.du import tree_bytes
from istota.sandbox.confined import open_dir, open_or_create_dirs, open_regular_at, valid_part
from istota.workspace.listing import ListingError, list_dir
from istota.workspace.writes import delete_entry, replace_file

logger = logging.getLogger(__name__)

#: The HTTP status each code is answered with.
STATUS = {
    "not_granted": 403,
    "bad_args": 400,
    "quota": 413,
    "rate_limited": 429,
    "stale_version": 409,
    "not_found": 404,
    "internal": 500,
}

RATE_WINDOW = timedelta(seconds=60)
KEY_MAX_CHARS = 1024
PATH_MAX_CHARS = 1024
LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 1000
#: A file larger than this is refused by ``files:own`` ``read``. A write is
#: bounded by the request body; a file the user or the agent put in ``data/``
#: is not, and this keeps one read from pulling it whole into the web process.
FILE_READ_MAX_BYTES = 10 * 1024 * 1024
_RECORDED_NAME_MAX = 200
ENCODINGS = ("utf8", "base64")


class BridgeError(Exception):
    """A call refused or failed with one of the kit's error codes."""

    def __init__(self, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return STATUS[self.code]


@dataclass(frozen=True)
class CallContext:
    config: object
    user_id: str
    app_id: str
    scope: object


def _bad_args(message: str) -> BridgeError:
    return BridgeError("bad_args", message)


# -- kv:own -------------------------------------------------------------------


def _key(args: dict) -> str:
    key = args.get("key")
    if not isinstance(key, str) or not key or len(key) > KEY_MAX_CHARS:
        raise _bad_args(f"key must be a non-empty string of at most {KEY_MAX_CHARS} characters.")
    return key


def _decode_value(text: str) -> object:
    # A value the agent wrote through the kv skill is JSON; anything else
    # comes back as the text it is, as `kv get` does.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _kv_get(ctx: CallContext, args: dict) -> object:
    key = _key(args)
    with framework_db.get_db(ctx.config.db_path) as conn:
        row = framework_db.kv_get(conn, ctx.user_id, kv_namespace(ctx.app_id), key)
    return None if row is None else _decode_value(row["value"])


def _kv_set(ctx: CallContext, args: dict) -> None:
    key = _key(args)
    if "value" not in args:
        raise _bad_args("set needs a value.")
    try:
        text = json.dumps(args["value"], ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise _bad_args("value must be a JSON value.") from None
    caps = ctx.config.apps
    size = len(text.encode("utf-8"))
    if size > caps.kv_value_max_bytes:
        raise BridgeError("quota", f"That value is over the {caps.kv_value_max_bytes}-byte limit.")
    namespace = kv_namespace(ctx.app_id)
    with framework_db.get_db(ctx.config.db_path) as conn:
        # One write transaction for the measurement and the write, so two
        # writes cannot each see room for themselves.
        conn.execute("BEGIN IMMEDIATE")
        used = framework_db.kv_namespace_bytes(conn, ctx.user_id, namespace, excluding_key=key)
        if used + len(key.encode("utf-8")) + size > caps.kv_max_bytes:
            conn.rollback()
            raise BridgeError("quota", f"This app's data is at its {caps.kv_max_bytes}-byte limit.")
        framework_db.kv_set(conn, ctx.user_id, namespace, key, text)
    return None


def _kv_delete(ctx: CallContext, args: dict) -> dict:
    key = _key(args)
    with framework_db.get_db(ctx.config.db_path) as conn:
        deleted = framework_db.kv_delete(conn, ctx.user_id, kv_namespace(ctx.app_id), key)
    return {"deleted": deleted}


def _int_arg(args: dict, name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad_args(f"{name} must be a whole number from {low} to {high}.")
    return value


def _kv_list(ctx: CallContext, args: dict) -> dict:
    prefix = args.get("prefix", "")
    if not isinstance(prefix, str) or len(prefix) > KEY_MAX_CHARS:
        raise _bad_args("prefix must be a string.")
    offset = _int_arg(args, "offset", 0, 0, 10**9)
    limit = _int_arg(args, "limit", LIST_DEFAULT_LIMIT, 1, LIST_MAX_LIMIT)
    with framework_db.get_db(ctx.config.db_path) as conn:
        rows = framework_db.kv_list(conn, ctx.user_id, kv_namespace(ctx.app_id))
    matching = [row for row in rows if row["key"].startswith(prefix)]
    items = [
        {"key": row["key"], "value": _decode_value(row["value"])}
        for row in matching[offset:offset + limit]
    ]
    return {"items": items, "total": len(matching)}


# -- files:own ----------------------------------------------------------------


def _path_parts(args: dict, *, allow_root: bool = False) -> list[str]:
    path = args.get("path", "")
    if not isinstance(path, str) or len(path) > PATH_MAX_CHARS:
        raise _bad_args("path must be a string.")
    if path == "":
        if allow_root:
            return []
        raise _bad_args("path names no file.")
    parts = path.split("/")
    for part in parts:
        if not valid_part(part) or part.startswith(".") or any(ord(c) < 0x20 for c in part):
            raise _bad_args(
                "path is relative to the app's data folder, with no empty, '.', '..' or hidden part."
            )
    return parts


def _encoding(args: dict) -> str:
    encoding = args.get("encoding", "utf8")
    if encoding not in ENCODINGS:
        raise _bad_args("encoding is utf8 or base64.")
    return encoding


def _data_root(ctx: CallContext) -> tuple[Path, list[str]]:
    root = ctx.config.workspace_root(ctx.user_id)
    if root is None:
        raise BridgeError("not_found", "There is no workspace to keep files in.")
    return root, [ctx.config.bot_dir_name, "apps", ctx.app_id, DATA]


def _open_folder(root: Path, parts: list[str]) -> int | None:
    """An fd on the folder, None when it does not exist, ``bad_args`` when something else is in the way."""
    try:
        fd = open_dir(root, *parts, raise_errors=True)
    except FileNotFoundError:
        return None
    except OSError:
        raise _bad_args("A part of that path is not a folder in the app's data folder.") from None
    if fd is None:
        raise _bad_args("That path is not in the app's data folder.")
    return fd


def _files_list(ctx: CallContext, args: dict) -> dict:
    parts = _path_parts(args, allow_root=True)
    root, data = _data_root(ctx)
    fd = _open_folder(root, data + parts)
    if fd is None:
        if not parts:
            return {"entries": [], "truncated": False}
        raise BridgeError("not_found", "No such folder.")
    os.close(fd)
    try:
        listing = list_dir(root, data + parts)
    except ListingError as exc:
        raise BridgeError("not_found", exc.message) from None
    entries = [
        {"name": e.name, "kind": e.kind, "size": e.size, "modified": e.modified}
        for e in listing.entries if not e.hidden
    ]
    return {"entries": entries, "truncated": listing.truncated}


def _files_read(ctx: CallContext, args: dict) -> str:
    parts = _path_parts(args)
    encoding = _encoding(args)
    root, data = _data_root(ctx)
    *dirs, leaf = parts
    dir_fd = _open_folder(root, data + dirs)
    if dir_fd is None:
        raise BridgeError("not_found", "No such file.")
    try:
        try:
            info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise BridgeError("not_found", "No such file.") from None
        if not stat.S_ISREG(info.st_mode):
            raise _bad_args("That path is not a file.")
        if info.st_size > FILE_READ_MAX_BYTES:
            raise BridgeError("quota", f"That file is over the {FILE_READ_MAX_BYTES}-byte read limit.")
        fd = open_regular_at(dir_fd, leaf)
        if fd is None:
            raise _bad_args("That path is not a file.")
        with os.fdopen(fd, "rb") as file:
            content = file.read(FILE_READ_MAX_BYTES + 1)
    finally:
        os.close(dir_fd)
    if len(content) > FILE_READ_MAX_BYTES:
        raise BridgeError("quota", f"That file is over the {FILE_READ_MAX_BYTES}-byte read limit.")
    if encoding == "base64":
        return base64.b64encode(content).decode("ascii")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad_args("That file is not UTF-8 text; read it with encoding base64.") from None


def _data_bytes(root: Path, data: list[str]) -> int:
    fd = _open_folder(root, data)
    if fd is None:
        return 0
    os.close(fd)
    # Measured by name after the no-follow open proved the folder is no link;
    # a swap in between can only make the count wrong, not reach other data.
    return tree_bytes(root.joinpath(*data))


def data_folder_bytes(config, user_id: str, app_id: str) -> int:
    """What the ``files:own`` cap counts for the app's ``data/`` folder: 0 when there is none.

    Raises :class:`BridgeError` (``bad_args`` or ``not_found``) when the folder
    is a link, something else is in its way, or there is no workspace.
    """
    root, data = _data_root(CallContext(config=config, user_id=user_id, app_id=app_id, scope=None))
    return _data_bytes(root, data)


def _files_write(ctx: CallContext, args: dict) -> dict:
    parts = _path_parts(args)
    encoding = _encoding(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise _bad_args("content must be a string.")
    try:
        if encoding == "base64":
            payload = base64.b64decode(content, validate=True)
        else:
            payload = content.encode("utf-8")
    except (binascii.Error, ValueError):
        raise _bad_args(f"content is not valid {encoding}.") from None
    root, data = _data_root(ctx)
    cap = ctx.config.apps.files_max_bytes
    if _data_bytes(root, data) + len(payload) > cap:
        raise BridgeError("quota", f"This app's data folder is at its {cap}-byte limit.")
    *dirs, leaf = parts
    try:
        os.close(open_or_create_dirs(root, data + dirs))
    except (OSError, ValueError):
        raise _bad_args("A part of that path is not a folder in the app's data folder.") from None
    try:
        replace_file(root, data + dirs, leaf, payload)
    except IsADirectoryError:
        raise _bad_args("That path is a folder.") from None
    except ListingError:
        raise BridgeError("not_found", "No such folder.") from None
    return {"path": "/".join(parts), "size": len(payload)}


def _files_delete(ctx: CallContext, args: dict) -> dict:
    parts = _path_parts(args)
    root, data = _data_root(ctx)
    *dirs, leaf = parts
    fd = _open_folder(root, data + dirs)
    if fd is None:
        raise BridgeError("not_found", "No such file.")
    os.close(fd)
    try:
        delete_entry(root, data + dirs, leaf)
    except ListingError as exc:
        code = "not_found" if exc.status == 404 else "bad_args"
        raise BridgeError(code, exc.message) from None
    return {"deleted": True}


Handler = Callable[[CallContext, dict], object]

#: ``(capability family, op)`` to its handler. Stage 14 adds the reads and
#: stage 15 the ``skill`` operations.
HANDLERS: dict[tuple[str, str], Handler] = {
    ("kv:own", "get"): _kv_get,
    ("kv:own", "set"): _kv_set,
    ("kv:own", "delete"): _kv_delete,
    ("kv:own", "list"): _kv_list,
    ("files:own", "list"): _files_list,
    ("files:own", "read"): _files_read,
    ("files:own", "write"): _files_write,
    ("files:own", "delete"): _files_delete,
}


# -- dispatch -----------------------------------------------------------------


def _answer(config, user_id, conn, app, *, version, capability, op, args, when) -> object:
    if not isinstance(capability, str) or not isinstance(op, str) or not isinstance(args, dict):
        raise _bad_args("A call names a capability and an operation, with its arguments as an object.")
    if app["current_version"] != version:
        raise BridgeError("stale_version", "This app was updated. Reload it.")
    grants = apps_db.get_grants(conn, app["app_id"], version)
    if capability not in grants:
        raise BridgeError("not_granted", f"This app has not been given {capability}.")
    handler = HANDLERS.get((split_capability(capability).name, op))
    if handler is None:
        raise _bad_args(f"{capability} has no operation {op}.")

    since = when - RATE_WINDOW
    if apps_db.count_recent_calls(conn, app["app_id"], since) >= config.apps.calls_per_minute:
        oldest = apps_db.oldest_recent_call(conn, app["app_id"], since)
        retry_after = 1
        if oldest is not None:
            retry_after = max(1, math.ceil((oldest + RATE_WINDOW - when).total_seconds()))
        raise BridgeError("rate_limited", "This app is calling too often.", retry_after=retry_after)

    return handler(CallContext(config, user_id, app["app_id"], grants[capability]), args)


def _recorded_name(value: object) -> str:
    return value[:_RECORDED_NAME_MAX] if isinstance(value, str) else ""


def dispatch(
    config,
    user_id: str,
    conn,
    *,
    app_id: str,
    version: int,
    capability: object,
    op: object,
    args: object,
    now: datetime | None = None,
) -> object:
    """Check and answer one bridge call; returns its result or raises :class:`BridgeError`.

    ``conn`` is the user's ``apps.db``. The call is recorded and committed on
    every path, except for an app that is not installed, which has no rows to
    record against.
    """
    when = now or datetime.now(timezone.utc)
    app = apps_db.get_app(conn, app_id)
    if app is None:
        raise BridgeError("not_found", "This app is not installed.")
    status = "ok"
    try:
        return _answer(
            config, user_id, conn, app,
            version=version, capability=capability, op=op, args=args, when=when,
        )
    except BridgeError as exc:
        status = exc.code
        raise
    except Exception as exc:
        status = "internal"
        logger.error(
            "apps_bridge_internal app=%s version=%s capability=%s op=%s error=%s",
            app_id, version, _recorded_name(capability), _recorded_name(op), type(exc).__name__,
        )
        raise BridgeError("internal", "Something went wrong answering this call.") from None
    finally:
        apps_db.insert_call(
            conn,
            app_id=app_id,
            version=version,
            capability=_recorded_name(capability),
            op=_recorded_name(op),
            args=args,
            status=status,
            retention_days=config.apps.calls_retention_days,
            now=when,
        )
        conn.commit()
