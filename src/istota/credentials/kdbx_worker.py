"""Opening an untrusted KeePass file in a disposable child process.

The parent half, `open_group`, takes an installation-wide admission slot (an
`exclusive_lock` with no wait), spawns this module as a child holding that
lock's descriptor, writes the file and its unlock material down a pipe, reads
a bounded JSON answer and reaps the child before the slot is released. The
child half, `main`, arms a wall-clock alarm, caps its address space on Linux,
opens the file with `kdbx_import.open_database` and answers with one named
root group's subtree as plain data.

What the answer means belongs to the caller: this module knows nothing about
installation backups beyond a group name and the limits it is handed.

**The slot outlives the parent.** The child inherits the locked descriptor
(`pass_fds`) and keeps it until it exits, so a parent killed mid-decrypt
leaves the slot held until the orphan's own alarm ends it, and no newly
admitted worker overlaps it. `open_group` never leaves the lock context
before the child is reaped, since the context's `LOCK_UN` releases every copy.

**Synchronous, run on a thread.** `open_group_async` runs `open_group` with
`asyncio.to_thread`, so cancelling the awaiting request leaves the thread to
finish: the child is reaped and the slot released by the thread, never by a
cancellation callback while the child still runs.

Nothing here logs the file, the password, the key file or the answer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import json
import math
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

TIMEOUT_SECONDS = 30.0
#: The child's address-space cap on Linux, set before the file is opened.
ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
#: The largest request the child reads: an 8 MiB file in base64 plus the
#: password, the key file and the JSON around them.
REQUEST_MAX_BYTES = 12 * 1024 * 1024
#: Per-entry JSON keys and punctuation, and the envelope, in the output bound.
_ENTRY_OVERHEAD_BYTES = 512
_ENVELOPE_BYTES = 64 * 1024


class WorkerError(Exception):
    """The worker did not produce an answer. `code` is the fixed reason."""

    code = "failed"


class WorkerBusy(WorkerError):
    code = "busy"


class WorkerUnavailable(WorkerError):
    """The admission anchor could not be opened or locked."""

    code = "unavailable"


class WorkerTimeout(WorkerError):
    code = "timeout"


class WorkerFailed(WorkerError):
    """A failed spawn, a crash, or an answer that is not the protocol's."""

    code = "failed"


class WorkerTooLarge(WorkerError):
    """The file holds more than the limits allow. `limit` names which."""

    code = "too_large"

    def __init__(self, limit: str):
        self.limit = limit
        super().__init__(f"over the {limit} limit")


@dataclasses.dataclass(frozen=True)
class WorkerLimits:
    """What the child may return: entries in the group, subgroup depth below
    it, UTF-8 bytes of every string, and decoded attachment bytes."""

    entries: int
    depth: int
    text_bytes: int
    attachment_bytes: int

    def output_bytes(self) -> int:
        """The most the answer can take on the wire. A string's JSON escape is
        at most six bytes per UTF-8 byte (`\\u001f`); an attachment is base64."""
        return (
            6 * self.text_bytes
            + 4 * math.ceil(self.attachment_bytes / 3)
            + _ENTRY_OVERHEAD_BYTES * (self.entries + 1)
            + _ENVELOPE_BYTES
        )

    def as_json(self) -> dict:
        return dataclasses.asdict(self)


def default_command(timeout: float = TIMEOUT_SECONDS) -> list[str]:
    """This module as a child of this interpreter, isolated from the working
    directory and user site, with its alarm at the parent's deadline."""
    return [
        sys.executable, "-I", "-m", "istota.credentials.kdbx_worker",
        "--deadline", str(max(1, math.ceil(timeout))),
    ]


def _child_env() -> dict[str, str]:
    # The web process's environment holds the stack's credentials; the child
    # needs none of them.
    return {"PATH": os.defpath, "LANG": "C.UTF-8"}


def _request(data: bytes, password: str | None, keyfile: bytes | None, group: str,
             limits: WorkerLimits) -> bytes:
    return json.dumps({
        "data": base64.b64encode(data).decode("ascii"),
        "password": password,
        "keyfile": base64.b64encode(keyfile).decode("ascii") if keyfile is not None else None,
        "group": group,
        "limits": limits.as_json(),
    }).encode("utf-8")


def _write_all(stream, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _read_bounded(stream, cap: int, deadline: float) -> bytes:
    """Up to `cap + 1` bytes until EOF; `WorkerTimeout` at `deadline`."""
    chunks: list[bytes] = []
    size = 0
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while size <= cap:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerTimeout("no answer before the deadline")
            if not selector.select(remaining):
                continue
            chunk = os.read(stream.fileno(), min(65536, cap + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    return b"".join(chunks)


def _run(argv: list[str], request: bytes, lock_fd: int, cap: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(lock_fd,),
            env=_child_env(),
            close_fds=True,
        )
    except (OSError, ValueError):
        raise WorkerFailed("the worker could not start") from None
    writer = threading.Thread(target=_write_all, args=(process.stdin, request), daemon=True)
    try:
        writer.start()
        output = _read_bounded(process.stdout, cap, deadline)
        if len(output) > cap:
            raise WorkerTooLarge("output")
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise WorkerTimeout("the worker did not exit before the deadline") from None
        if process.returncode != 0:
            raise WorkerFailed("the worker failed")
        return output
    finally:
        # Reaped before the caller leaves the lock context, on every path.
        if process.poll() is None:
            process.kill()
        process.wait()
        writer.join()
        with contextlib.suppress(OSError):
            process.stdout.close()


def _answer(output: bytes) -> dict:
    from istota.credentials import kdbx_import

    try:
        reply = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WorkerFailed("the worker's answer is not the protocol's") from None
    if not isinstance(reply, dict):
        raise WorkerFailed("the worker's answer is not the protocol's")
    if reply.get("ok") is True:
        if not isinstance(reply.get("matches"), int) or not (
            reply.get("group") is None or isinstance(reply.get("group"), dict)
        ):
            raise WorkerFailed("the worker's answer is not the protocol's")
        return reply
    error = reply.get("error")
    if error == "locked":
        raise kdbx_import.VaultLocked("the supplied credentials do not open this file")
    if error == "corrupt":
        raise kdbx_import.VaultCorrupt("the file is not a readable KeePass database")
    if error == "library_missing":
        raise kdbx_import.VaultLibraryMissing("the 'vault' extra is not installed on this host")
    if error == "too_large":
        limit = reply.get("limit")
        raise WorkerTooLarge(limit if limit in ("entries", "depth", "text", "attachments") else "output")
    raise WorkerFailed("the worker refused the request")


def open_group(
    data: bytes, password: str | None, keyfile: bytes | None, *,
    group: str, limits: WorkerLimits, lock_path: Path,
    timeout: float = TIMEOUT_SECONDS, command: list[str] | None = None,
) -> dict:
    """`group`'s subtree out of the KeePass file `data`, opened in a child.

    Returns `{"matches": n, "group": {...} | None}`, where `matches` counts
    root groups named `group` and the subtree is the first. Raises
    `WorkerBusy` at once when another decrypt holds `lock_path`,
    `WorkerUnavailable` when the anchor cannot be used, `WorkerTimeout`,
    `WorkerTooLarge`, `WorkerFailed`, or the `kdbx_import` error for a wrong
    password or an unreadable file. `command` replaces the child, for tests.
    """
    from istota.lib.file_lock import exclusive_lock

    stack = contextlib.ExitStack()
    try:
        lock_fd = stack.enter_context(exclusive_lock(
            lock_path, timeout_seconds=0, nofollow=True,
            on_timeout=lambda _path: WorkerBusy("another backup is being opened"),
        ))
    except WorkerBusy:
        raise
    except OSError:
        raise WorkerUnavailable("the decrypt admission anchor cannot be used") from None
    with stack:
        output = _run(
            command or default_command(timeout),
            _request(data, password, keyfile, group, limits),
            lock_fd, limits.output_bytes(), timeout,
        )
    return _answer(output)


async def open_group_async(*args, **kwargs) -> dict:
    """`open_group` off the event loop. Cancelling the caller does not stop
    the thread, so the child is still reaped before the slot is released."""
    return await asyncio.to_thread(open_group, *args, **kwargs)


# --- the child ----------------------------------------------------------------


class _Limit(Exception):
    def __init__(self, limit: str):
        self.limit = limit


class _Budget:
    def __init__(self, limits: dict):
        self.limits = limits
        self.entries = 0
        self.text = 0
        self.attachments = 0

    def text_of(self, value) -> str:
        text = "" if value is None else str(value)
        self.text += len(text.encode("utf-8", "surrogatepass"))
        if self.text > self.limits["text_bytes"]:
            raise _Limit("text")
        return text


def _dump_entry(entry, budget: _Budget) -> dict:
    budget.entries += 1
    if budget.entries > budget.limits["entries"]:
        raise _Limit("entries")
    properties = {}
    protected = []
    for key, value in entry.custom_properties.items():
        name = budget.text_of(key)
        properties[name] = budget.text_of(value)
        if entry.is_custom_property_protected(key):
            protected.append(name)
    attachments = []
    for attachment in entry.attachments:
        payload = attachment.data or b""
        budget.attachments += len(payload)
        if budget.attachments > budget.limits["attachment_bytes"]:
            raise _Limit("attachments")
        attachments.append({
            "filename": budget.text_of(attachment.filename),
            "data": base64.b64encode(payload).decode("ascii"),
        })
    return {
        "title": budget.text_of(entry.title),
        "username": budget.text_of(entry.username),
        "password": budget.text_of(entry.password),
        "url": budget.text_of(entry.url),
        "notes": budget.text_of(entry.notes),
        "properties": properties,
        "protected": protected,
        "attachments": attachments,
        "history": len(entry.history or []),
    }


def _dump_group(group, budget: _Budget, depth: int, recyclebin) -> dict:
    entries = [_dump_entry(entry, budget) for entry in group.entries]
    subgroups = []
    for subgroup in group.subgroups:
        if recyclebin is not None and subgroup.uuid == recyclebin:
            continue
        if depth + 1 > budget.limits["depth"]:
            raise _Limit("depth")
        subgroups.append(_dump_group(subgroup, budget, depth + 1, recyclebin))
    return {"name": budget.text_of(group.name), "entries": entries, "groups": subgroups}


def _limits_of(value) -> dict | None:
    keys = ("entries", "depth", "text_bytes", "attachment_bytes")
    if not isinstance(value, dict) or set(value) != set(keys):
        return None
    if not all(isinstance(value[key], int) and not isinstance(value[key], bool) and value[key] >= 0
               for key in keys):
        return None
    return value


def _decode_request(raw: bytes) -> tuple[bytes, str | None, bytes | None, str, dict] | None:
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            return None
        data = base64.b64decode(request["data"], validate=True)
        password = request["password"]
        keyfile = request["keyfile"]
        if keyfile is not None:
            keyfile = base64.b64decode(keyfile, validate=True)
        group = request["group"]
        limits = _limits_of(request["limits"])
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, binascii.Error, RecursionError):
        return None
    if not (password is None or isinstance(password, str)) or not isinstance(group, str) or limits is None:
        return None
    return data, password, keyfile, group, limits


def _serve(raw: bytes) -> dict:
    from istota.credentials import kdbx_import

    decoded = _decode_request(raw)
    if decoded is None:
        return {"ok": False, "error": "request"}
    data, password, keyfile, group_name, limits = decoded
    try:
        kp = kdbx_import.open_database(data, password, keyfile=keyfile)
    except kdbx_import.VaultLocked:
        return {"ok": False, "error": "locked"}
    except kdbx_import.VaultLibraryMissing:
        return {"ok": False, "error": "library_missing"}
    except kdbx_import.VaultCorrupt:
        return {"ok": False, "error": "corrupt"}
    try:
        recyclebin = kdbx_import._recyclebin_uuid(kp)
        matches = [
            group for group in kp.root_group.subgroups
            if (recyclebin is None or group.uuid != recyclebin)
            and str(group.name or "").strip() == group_name
        ]
        dumped = _dump_group(matches[0], _Budget(limits), 0, recyclebin) if matches else None
    except _Limit as exc:
        return {"ok": False, "error": "too_large", "limit": exc.limit}
    except Exception:
        return {"ok": False, "error": "corrupt"}
    return {"ok": True, "matches": len(matches), "group": dumped}


def main(argv: list[str] | None = None) -> int:
    import signal

    args = list(sys.argv[1:] if argv is None else argv)
    deadline = int(TIMEOUT_SECONDS)
    if len(args) == 2 and args[0] == "--deadline" and args[1].isdigit():
        deadline = max(1, int(args[1]))
    # Default SIGALRM disposition ends the process even inside the KDF's C code,
    # so an orphaned child still exits and releases the inherited lock.
    signal.alarm(deadline)
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    raw = sys.stdin.buffer.read(REQUEST_MAX_BYTES + 1)
    if len(raw) > REQUEST_MAX_BYTES:
        reply = {"ok": False, "error": "request"}
    else:
        reply = _serve(raw)
    sys.stdout.buffer.write(json.dumps(reply, separators=(",", ":")).encode("ascii"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
