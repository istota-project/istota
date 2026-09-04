"""The daemon's half of the tool server: the spawn, the client, six proxies.

``NativeBrain`` used to execute the model's tool calls two different ways —
``Read``/``Write``/``Edit``/``Grep``/``Glob`` on daemon worker threads behind
``ToolEnv``'s path allowlist, and ``Bash`` inside a fresh bwrap namespace per
call. This module replaces both with one namespace per task attempt:
``python -m istota.tool_server``, spawned once through
``build_bwrap_cmd(..., profile=NATIVE)``, holding all six tools, reached over a
socket the model cannot name (ISSUE-389).

What that buys, and why it is not just a refactor: the five file tools enter a
namespace for the first time, so a hostile path is *absent* rather than
refused; the check-then-open race in ``ToolEnv.resolve`` stops mattering,
because the host path an ancestor swap would reach is not in the namespace;
``Grep``'s regular expressions and ``Read``'s opens leave the daemon process;
everything ``Bash`` forks is in the task cgroup; and ``/tmp`` and background
processes live for the attempt instead of dying with each call.

``ToolEnv``'s roots are still sent and still enforced, in the server. They are
the error-message layer now — "outside the allowed workspace" is a better
answer than ENOENT — rather than the boundary.

The transport
-------------

An inherited ``AF_UNIX``/``SOCK_STREAM`` socketpair. Nothing nameable, so there
is nothing for the model to replace and no peer to authenticate, and
``close_fds`` keeps it out of every Bash child (measured: a child started
inside the sandbox with ``asyncio.create_subprocess_exec`` reports the
descriptor absent). bubblewrap preserves an inherited descriptor across its own
fork and exec, including under ``--unshare-pid --unshare-user --disable-userns
--unshare-net --die-with-parent`` — measured on Debian 13 with bubblewrap
0.12.0 before this was built.

**The descriptor's number is passed in argv rather than pinned to 3.** Both are
sound: the security property is that the descriptor is unnameable in the
filesystem, not that it is numbered 3. Pinning it means ``os.dup2(raw, 3)`` in
the parent — the only correct place, since CPython's ``child_exec`` closes
every descriptor at or above 3 that is not in ``fds_to_keep`` *after* it calls
``preexec_fn``, so the natural "dup2 inside preexec_fn" fails with ``EBADF`` at
the server's first read. But this parent is a long-lived daemon running tasks
on worker threads, and fd 3 there belongs to whatever opened it — a SQLite
connection, a log handle, another task's socket — so a dup2 onto it would close
a file another thread is using. Passing the number keeps ``pass_fds`` (which is
what puts it in ``fds_to_keep``) and touches nothing else.

Failure
-------

Three terminal states, and the third is the one worth being exact about. A
``result`` frame is the ``ToolResult``. An aborted call returns an error result
and the loop's existing abort handling takes over. A dead server, a ``fatal``,
or a malformed frame **fails the attempt** — every in-flight call gets an error
result so nothing raises into the loop, the loop's abort is set so the run
stops promptly, and ``failure`` is left set for ``NativeBrain`` to turn into a
failed ``BrainResult``. Degrading each tool call to an error result and
carrying on is the tempting alternative and it is wrong: the model would
narrate around a broken sandbox and answer confidently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from istota import task_cgroup, tool_server_protocol as proto
from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import ImageContent, TextContent, ToolSchema
from istota.process_group import kill_process_group

from .bash import BASH_SCHEMA
from .files import (
    EDIT_SCHEMA,
    GLOB_SCHEMA,
    GREP_SCHEMA,
    READ_SCHEMA,
    WRITE_SCHEMA,
    prepare_edit_arguments,
)

logger = logging.getLogger(__name__)

# The six, in `build_default_tools` order, each paired with the execution mode
# the real tool declares. `tests/test_tool_schema_parity.py` compares this
# table against the factories field for field, so a mode changed on one side
# and not the other is a failure rather than a silently serialized batch.
_PROXIED: tuple[tuple[ToolSchema, str], ...] = (
    (READ_SCHEMA, "parallel"),
    (WRITE_SCHEMA, "sequential"),
    (EDIT_SCHEMA, "sequential"),
    (GREP_SCHEMA, "parallel"),
    (GLOB_SCHEMA, "parallel"),
    (BASH_SCHEMA, "sequential"),
)

# Only Edit has one, and it is the *same object* the real tool passes, not a
# second coercion shim. See `files.prepare_edit_arguments`.
_PREPARE: dict[str, Callable[[dict], dict]] = {"Edit": prepare_edit_arguments}

# The failure text a signal-killed server produces must keep this prefix:
# `scheduler._is_shutdown_collateral` is `_shutdown_requested and
# is_signal_termination(text)`, and that predicate is a `startswith` on exactly
# this string. A `systemctl restart` SIGTERMs the whole cgroup, tool server
# included, so without the prefix a routine deploy would fail and retry every
# in-flight native task instead of requeuing it (ISSUE-191).
#
# Restated rather than imported from `brain/claude_code.py`, which would close
# an import cycle — `istota.brain.claude_code` runs `istota.brain.__init__`,
# which imports `native`, which is the module that imports this package.
# `tests/test_tool_server.py` holds the two strings equal, which is the same
# trade `devbox_exec_protocol.SIGPIPE_NOTE` and `native._MAX_IMAGE_BYTES` make.
_TERMINATED_PREFIX = "Claude Code was terminated by "


# How long a teardown waits for a `shutdown` to reach the socket, and how long
# either side waits to reap. Both are small on purpose: they sit on paths that
# must always complete, and everything they guard has a kill behind it.
_SHUTDOWN_SEND_TIMEOUT_SECONDS = 1.0
_REAP_TIMEOUT_SECONDS = 5.0


class ToolServerError(Exception):
    """The server could not be started, or died. Fails the attempt."""


def attempt_failure_text(failure: str) -> str:
    """The `result_text` a tool-server failure becomes.

    Ordinarily it names the tool server, because a bare provider-shaped string
    in the task's error column would send the reader looking in the wrong
    subsystem. **A signal death is the exception, and the exception is the
    whole point**: `scheduler._is_shutdown_collateral` is `_shutdown_requested
    and is_signal_termination(text)`, and that predicate is a `startswith`.
    Prefixing "the tool server failed:" onto it moves the marker off the front
    of the string, `startswith` stops matching, and a `systemctl restart` — the
    auto-update cron issues one on every new commit — fails and retries every
    in-flight native task instead of requeuing it. So a signal death is passed
    through verbatim, which is exactly what `ClaudeCodeBrain._signal_result`
    puts in the same field for the same reason.
    """
    if failure.startswith(_TERMINATED_PREFIX):
        return failure
    return f"The native brain's tool server failed: {failure}"


def content_to_wire(content) -> list[dict[str, Any]]:
    """``ToolResult.content`` → the plain dicts the protocol carries.

    Here rather than in ``tool_server_protocol`` so that module stays
    stdlib-only, and in *one* place rather than one per end: the server imports
    this function, so a block shape can never be written by one side in a form
    the other does not read.
    """
    out: list[dict[str, Any]] = []
    for block in content or ():
        if isinstance(block, ImageContent):
            out.append({
                "type": proto.CONTENT_IMAGE,
                "media_type": block.media_type,
                "data": block.data,
                "display_name": block.display_name,
            })
        else:
            out.append({"type": proto.CONTENT_TEXT, "text": getattr(block, "text", "")})
    return out


def content_from_wire(blocks) -> list[TextContent | ImageContent]:
    """The inverse. An unknown block type is dropped rather than raised on —
    both ends ship from one tree, so this can only be a future format, and
    losing one block beats failing the attempt over it."""
    out: list[TextContent | ImageContent] = []
    for block in blocks or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == proto.CONTENT_IMAGE:
            out.append(ImageContent(
                media_type=str(block.get("media_type") or ""),
                data=str(block.get("data") or ""),
                display_name=str(block.get("display_name") or ""),
            ))
        elif block.get("type") == proto.CONTENT_TEXT:
            out.append(TextContent(text=str(block.get("text") or "")))
    return out


def hello_payload(
    *,
    cwd: Path,
    subprocess_env: dict[str, str] | None,
    read_roots: tuple[Path, ...] | None,
    write_roots: tuple[Path, ...] | None,
    write_denied_roots: tuple[Path, ...],
    deferred_dir: Path | None,
    bash_timeout_seconds: int,
    max_output_bytes: int,
    max_read_lines: int,
    max_read_bytes: int,
    bash_spill_full_output: bool,
) -> dict[str, Any]:
    """The ``hello`` frame: everything the server needs to build its ``ToolEnv``.

    A function rather than an inline literal because two things read it — the
    spawn, and the tests that assert a request's confinement roots reach the
    tools without standing up a subprocess.
    """
    return {
        "type": proto.MSG_HELLO,
        "protocol": proto.PROTOCOL_VERSION,
        "cwd": str(cwd),
        "subprocess_env": dict(subprocess_env) if subprocess_env is not None else None,
        "read_roots": [str(p) for p in read_roots] if read_roots is not None else None,
        "write_roots": [str(p) for p in write_roots] if write_roots is not None else None,
        "write_denied_roots": [str(p) for p in (write_denied_roots or ())],
        "deferred_dir": str(deferred_dir) if deferred_dir is not None else None,
        "bash_timeout_seconds": int(bash_timeout_seconds),
        "max_output_bytes": int(max_output_bytes),
        "max_read_lines": int(max_read_lines),
        "max_read_bytes": int(max_read_bytes),
        "bash_spill_full_output": bool(bash_spill_full_output),
    }


class RemoteToolServer:
    """One tool server process, its socket, and the calls in flight on it."""

    def __init__(
        self,
        proc,
        sock: socket.socket,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        loop_abort: asyncio.Event | None = None,
    ) -> None:
        self._proc = proc
        self._sock = sock
        self._reader = reader
        self._writer = writer
        self._loop_abort = loop_abort
        self._pending: dict[str, asyncio.Future] = {}
        self._updates: dict[str, Any] = {}
        # Strong references to in-flight update deliveries, and the tail of
        # each call's chain. See `_queue_update`.
        self._update_tasks: set[asyncio.Task] = set()
        self._update_chain: dict[str, asyncio.Task] = {}
        # Monotonic, never `len(self._pending)`: that shrinks as calls finish,
        # so a third call could rebuild a key a live second call still holds
        # and silently take over its future — which is the exact collision the
        # suffix exists to prevent.
        self._next_key = 0
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr = bytearray()
        self._stopping = False
        self._closed = False
        self._write_lock = asyncio.Lock()
        self.tools: list[str] = []
        # Set once, by whichever of the reader, the process watcher or a send
        # notices first. Read by NativeBrain after the loop returns, which is
        # why it outlives `aclose`.
        self.failure: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def pid(self) -> int:
        return self._proc.pid

    def _fail(self, reason: str) -> None:
        """Record the first failure, error every in-flight call, stop the run."""
        if self._stopping or self.failure is not None:
            return
        self.failure = reason
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(ToolResult(
                    content=[TextContent(text=f"Tool server failed: {reason}")],
                    is_error=True,
                ))
        self._pending.clear()
        # The loop is mid-turn and would otherwise keep asking a dead server
        # for tools. Aborting stops it at the next boundary; NativeBrain's
        # post-loop `failure` check is what converts that into a failed
        # attempt rather than a cancellation.
        if self._loop_abort is not None and not self._loop_abort.is_set():
            self._loop_abort.set()

    async def _read_frames(self, decoder: "proto.FrameDecoder | None" = None) -> None:
        decoder = decoder if decoder is not None else proto.FrameDecoder()
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    if self._stopping:
                        # `aclose` asked it to go; a half-written frame on the
                        # way out is not a failure to report.
                        return
                    # A partial frame at EOF is the peer dying mid-write,
                    # not a codec disagreement, so it must not outrank the
                    # death reason: reporting "protocol error: stream
                    # ended mid-frame" for a SIGTERM would lose the prefix
                    # `_is_shutdown_collateral` matches on, which is the
                    # same regression by another route. The truncation is
                    # still recorded, as a suffix on the real cause.
                    truncated = ""
                    try:
                        decoder.close()
                    except proto.ProtocolError as exc:
                        truncated = f" ({exc})"
                    reason = await self._death_reason("the tool server exited")
                    self._fail(reason + truncated)
                    return
                for msg in decoder.feed(data):
                    self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except proto.ProtocolError as exc:
            self._fail(f"tool server protocol error: {exc}")
        except Exception as exc:  # noqa: BLE001 — the reader must not escape
            self._fail(f"tool server connection failed: {exc}")

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == proto.MSG_UPDATE:
            key = str(msg.get("id"))
            on_update = self._updates.get(key)
            if on_update is not None:
                self._queue_update(key, on_update, str(msg.get("text") or ""))
            return
        if kind == proto.MSG_RESULT:
            fut = self._pending.pop(str(msg.get("id")), None)
            self._updates.pop(str(msg.get("id")), None)
            self._update_chain.pop(str(msg.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(ToolResult(
                    content=content_from_wire(msg.get("content")),
                    # Never carried across: `agent/loop.py` is its only reader
                    # and only for the after-hook override.
                    details=None,
                    is_error=bool(msg.get("is_error")),
                    terminate=bool(msg.get("terminate")),
                ))
            return
        if kind == proto.MSG_FATAL:
            self._fail(f"tool server reported a fatal error: {msg.get('message')}")
            return
        if kind == proto.MSG_READY:
            return  # consumed by the handshake; a second one is noise
        self._fail(f"unexpected message from the tool server: {kind!r}")

    def _queue_update(self, key: str, on_update, text: str) -> None:
        """Deliver one `update` chunk without blocking the reader.

        Not a bare `ensure_future`, for two reasons that both bite. **Ordering
        per call**: two tasks each `await on_update(...)`, the executor's
        callback writes a `task_events` row, and they interleave at the first
        await — so a Bash command's streamed output could reach the surface
        out of order, which the in-process version it replaced could not do.
        Each call's updates are therefore chained behind the previous one.
        **And a reference**: asyncio may garbage-collect a task nothing holds,
        which loses a chunk silently.

        Still not awaited in the reader, deliberately: one surface's slow write
        would otherwise stall every other call's *result* frames behind it,
        which is a worse failure than a late progress chunk.
        """
        previous = self._update_chain.get(key)

        async def _chained() -> None:
            if previous is not None:
                with contextlib.suppress(BaseException):
                    await previous
            await _safe_update(on_update, text)

        task = asyncio.ensure_future(_chained())
        self._update_chain[key] = task
        self._update_tasks.add(task)
        task.add_done_callback(self._update_tasks.discard)

    async def _death_reason(self, prefix: str) -> str:
        """Why the server is gone, in the wording the scheduler classifies on.

        **It waits for the child first, and that await is the whole
        correctness of the signal case.** EOF on the socketpair is delivered by
        the selector the moment the child's fd closes, while `returncode` is
        set from a *different* callback — asyncio's child watcher does a
        blocking `waitpid` on its own thread and then hands the result back
        through `call_soon_threadsafe`. Read without waiting, `returncode` is
        almost always still `None`, the signal branch below never runs, and the
        `_TERMINATED_PREFIX` this class exists to preserve is never produced —
        so `_is_shutdown_collateral` stays False and a deploy restart fails and
        retries every in-flight native task instead of requeuing it. That is
        ISSUE-191 reappearing through a race rather than through wording.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
        rc = self._proc.returncode
        if rc is not None and rc < 0:
            signum = -rc
            try:
                name = signal.Signals(signum).name
            except ValueError:
                name = "signal"
            # Keeps `is_signal_termination`'s prefix — see the module note.
            return f"{_TERMINATED_PREFIX}{name} (signal {signum})"
        tail = bytes(self._stderr[-2000:]).decode("utf-8", "replace").strip()
        detail = f" (exit {rc})" if rc is not None else ""
        return f"{prefix}{detail}{': ' + tail if tail else ''}"

    async def _drain_stderr(self) -> None:
        """Keep the pipe empty and the last of it for the failure message.

        Bounded: a server that writes forever must not grow the daemon, and the
        useful part of a Python traceback is its tail.
        """
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self._stderr.extend(chunk)
                del self._stderr[:-8192]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def aclose(self) -> None:
        """Shut the server down: ``shutdown``, then the graceful window, then kill.

        Idempotent, and it never raises: it is called from a ``finally`` on
        every path including a cancelled attempt.
        """
        if self._closed:
            return
        self._stopping = True
        try:
            # **Bounded.** `_send` takes the write lock and then `drain()`s,
            # and neither is bounded on its own: a concurrent `call` or an
            # abort watcher parked inside `drain()` against a server that has
            # stopped reading would hold the lock indefinitely, and this is the
            # first thing on a teardown path that must always complete. A
            # `shutdown` that could not be delivered costs the graceful window
            # and nothing else — the kill below is what actually ends it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._send({"type": proto.MSG_SHUTDOWN}),
                    timeout=_SHUTDOWN_SEND_TIMEOUT_SECONDS,
                )
            with contextlib.suppress(Exception):
                self._writer.close()
            try:
                await asyncio.wait_for(
                    self._proc.wait(), timeout=proto.SHUTDOWN_GRACE_SECONDS
                )
            except BaseException:  # noqa: BLE001 — CancelledError included
                # The group, not the process: everything Bash forked is in it,
                # and `--die-with-parent` only fires when the *daemon* goes.
                # `BaseException` rather than `Exception` because this is
                # called from a `finally` that runs on the cancellation path by
                # design, and a `CancelledError` delivered in the graceful
                # window would otherwise leave the bwrap process and its whole
                # build tree alive until the daemon exited.
                with contextlib.suppress(BaseException):
                    kill_process_group(self._proc.pid)
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(self._proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
            for task in (self._reader_task, self._stderr_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            for task in list(self._update_tasks):
                task.cancel()
            with contextlib.suppress(Exception):
                self._sock.close()
        finally:
            # In a `finally`, so a cancellation partway through cannot leave
            # `_closed` True with the process still alive and every later call
            # a no-op — which is the same leak the branch above exists to stop,
            # reached through the flag instead of through the wait.
            self._closed = True

    # -- calls -------------------------------------------------------------

    async def _send(self, message: dict) -> None:
        async with self._write_lock:
            self._writer.write(proto.encode(message))
            await self._writer.drain()

    async def call(
        self,
        tool: str,
        call_id: str,
        args: dict,
        on_update,
        abort: asyncio.Event | None,
    ) -> ToolResult:
        if self.failure is not None:
            return ToolResult(
                content=[TextContent(text=f"Tool server failed: {self.failure}")],
                is_error=True,
            )
        # The loop reuses a tool_call id at most once, but a defensive suffix
        # costs nothing and a collision would deliver one call's result to the
        # other's future.
        self._next_key += 1
        key = f"{call_id}:{self._next_key}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        if on_update is not None:
            self._updates[key] = on_update
        watcher: asyncio.Task | None = None
        try:
            await self._send({
                "type": proto.MSG_CALL, "id": key, "tool": tool, "args": args,
            })
            if abort is not None:
                watcher = asyncio.ensure_future(self._forward_abort(key, abort))
            return await fut
        except proto.ProtocolError as exc:
            # An argument the codec cannot carry (a non-serializable value off
            # the model's JSON, or one over the frame cap). The server is
            # healthy; this call is not.
            return ToolResult(
                content=[TextContent(text=f"Tool call could not be sent: {exc}")],
                is_error=True,
            )
        except asyncio.CancelledError:
            # The daemon side is going; the server does not know that. Without
            # this the command keeps running — a build, a `sleep 30` — until
            # `aclose` reaches it, which is the whole rest of the teardown
            # away and, on the path where `aclose` itself is cancelled, never.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._send({"type": proto.MSG_ABORT, "id": key}),
                    timeout=_SHUTDOWN_SEND_TIMEOUT_SECONDS,
                )
            raise
        except Exception as exc:  # noqa: BLE001 — nothing raises into the loop
            self._fail(f"tool call failed: {exc}")
            return ToolResult(
                content=[TextContent(text=f"Tool server failed: {exc}")],
                is_error=True,
            )
        finally:
            self._pending.pop(key, None)
            self._updates.pop(key, None)
            self._update_chain.pop(key, None)
            if watcher is not None:
                watcher.cancel()

    async def _forward_abort(self, key: str, abort: asyncio.Event) -> None:
        try:
            await abort.wait()
            await self._send({"type": proto.MSG_ABORT, "id": key})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — best effort; the kill paths remain
            logger.debug("could not forward an abort to the tool server", exc_info=True)


async def _safe_update(on_update, text: str) -> None:
    try:
        await on_update(text)
    except Exception:  # noqa: BLE001 — progress is best-effort
        logger.debug("tool server on_update raised", exc_info=True)


def server_command() -> list[str]:
    """The raw argv, before the sandbox wrap.

    The interpreter and the package are both bound into the namespace already:
    ``build_bwrap_cmd`` ro-binds the venv and the source tree at their own
    paths for every profile. Which is why this is not ``sys.executable`` —
    ``executor.sandbox_interpreter`` names the interpreter as it exists *in
    there*, under the venv path that is actually bound, rather than under a
    symlink to it that no bind carries in.
    """
    # Function scope, and not for a cycle — there is none today, measured.
    # `istota.tool_server` imports this module's package from *inside* the
    # namespace, and `executor` pulls the brains and their provider clients in
    # behind it. The daemon has paid for that graph; the tool server has not,
    # and it starts one per task attempt.
    from ...executor import sandbox_interpreter

    return [str(sandbox_interpreter()), "-m", "istota.tool_server"]


async def start_tool_server(
    hello: dict[str, Any],
    *,
    sandbox_wrap: Callable[[list[str]], list[str]] | None = None,
    task_cgroup_path: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_pid: Callable[[int], None] | None = None,
    loop_abort: asyncio.Event | None = None,
    startup_timeout: float = proto.STARTUP_TIMEOUT_SECONDS,
) -> RemoteToolServer:
    """Spawn one server, hand it ``hello``, wait for ``ready``.

    Raises ``ToolServerError`` if it cannot be started or does not answer.
    """
    # Before the socketpair, so nothing is allocated yet if it raises. It was a
    # list literal until `sandbox_interpreter` went in behind it; inside the
    # `try` below, whose only handler is `(OSError, ValueError)`, anything else
    # it raised would escape the `ToolServerError` contract *and* leak `parent`,
    # since that handler is the sole place the parent end is closed.
    base_cmd = server_command()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    raw = child.detach()  # take the fd out of the socket object; we own it now
    proc = None
    try:
        os.set_inheritable(raw, True)
        cmd = base_cmd + ["--fd", str(raw)]
        if sandbox_wrap is not None:
            cmd = sandbox_wrap(cmd)
        # Placement from `preexec_fn`, not a write after the spawn: membership
        # is inherited at fork and bwrap forks during namespace setup, so a pid
        # moved afterwards leaves everything it already forked outside the
        # group for good (ISSUE-285). `placement` yields None where the
        # deployment has no delegated subtree, which makes `preexec_fn=None`
        # and the spawn identical to what it would otherwise have been.
        with task_cgroup.placement(task_cgroup_path) as place_in_cgroup:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                # In `fds_to_keep`, which is the whole mechanism: CPython
                # closes every other descriptor at or above 3 *after*
                # `preexec_fn` runs.
                pass_fds=(raw,),
                # Its own group, so `aclose` can take the Bash trees with it.
                start_new_session=True,
                preexec_fn=place_in_cgroup,
            )
        if place_in_cgroup is not None and task_cgroup_path is not None:
            task_cgroup.verify_placement(proc.pid, task_cgroup_path)
    except (OSError, ValueError) as exc:
        parent.close()
        raise ToolServerError(f"could not start the tool server: {exc}") from exc
    finally:
        # One close, on the one path every branch passes through. Closing it in
        # the `except` as well would be a double close, and a second close of a
        # number the daemon has since reused belongs to somebody else.
        # The parent must not keep the child's end: while it holds one, the
        # server's death produces no EOF here and a dead server would read as
        # a slow one until the task's own clock ran out.
        with contextlib.suppress(OSError):
            os.close(raw)

    try:
        reader, writer = await asyncio.open_connection(sock=parent)
    except BaseException:
        # Outside the block above, so nothing there closes either of these.
        with contextlib.suppress(BaseException):
            kill_process_group(proc.pid)
        with contextlib.suppress(BaseException):
            parent.close()
        raise
    server = RemoteToolServer(proc, parent, reader, writer, loop_abort=loop_abort)
    server._stderr_task = asyncio.ensure_future(server._drain_stderr())
    handshake = proto.FrameDecoder()
    try:
        await server._send(hello)
        ready = await asyncio.wait_for(
            _await_ready(server, reader, handshake), timeout=startup_timeout
        )
    except asyncio.TimeoutError:
        detail = await _abandon(server)
        raise ToolServerError(
            f"the tool server did not become ready within {startup_timeout:.0f}s"
            f"{detail}"
        ) from None
    except ToolServerError:
        await _abandon(server)
        raise
    except Exception as exc:  # noqa: BLE001
        detail = await _abandon(server)
        raise ToolServerError(
            f"the tool server handshake failed: {exc}{detail}"
        ) from exc

    server.tools = [str(t) for t in (ready.get("tools") or [])]
    # The reader takes over the socket only after the handshake, so the two
    # never race for the same bytes — and it takes over the handshake's
    # *decoder* rather than starting a fresh one, which is the other half of
    # that. A `fatal` arriving in the same read as `ready` would otherwise be
    # dropped, and a partial frame left in the handshake buffer would leave a
    # new decoder reading the middle of a JSON payload as a 4-byte length:
    # a real failure reported with a fabricated cause.
    server._reader_task = asyncio.ensure_future(server._read_frames(handshake))
    if on_pid is not None:
        with contextlib.suppress(Exception):
            # The outer bwrap pid, which is what `!stop`, the web cancel
            # endpoint and `host_pressure.read_sandbox_shm` need — this brain
            # used to record none at all, so every native task carried
            # `worker_pid` 0.
            on_pid(proc.pid)
    return server


async def _await_ready(
    server: RemoteToolServer,
    reader: asyncio.StreamReader,
    decoder: proto.FrameDecoder,
) -> dict:
    """Read frames until ``ready``. Anything else is a failed start.

    The decoder is the caller's, and is handed on to ``_read_frames``: bytes
    that arrived in the same read as ``ready`` belong to the stream, not to
    this function.
    """
    while True:
        data = await reader.read(65536)
        if not data:
            decoder.close()
            raise ToolServerError(
                await server._death_reason("the tool server exited at startup")
            )
        for msg in decoder.feed(data):
            if msg.get("type") == proto.MSG_READY:
                got = msg.get("protocol")
                if got not in proto.SUPPORTED_PROTOCOLS:
                    raise ToolServerError(
                        f"the tool server speaks protocol {got!r}, this daemon speaks "
                        f"{sorted(proto.SUPPORTED_PROTOCOLS)}"
                    )
                return msg
            if msg.get("type") == proto.MSG_FATAL:
                raise ToolServerError(f"the tool server failed to start: {msg.get('message')}")
            raise ToolServerError(
                f"the tool server sent {msg.get('type')!r} before it was ready"
            )


async def _abandon(server: RemoteToolServer) -> str:
    """Tear down a server that never became usable, and say why if it said.

    Returns the tail of the server's stderr, already formatted for appending to
    a ``ToolServerError`` message, or ``""`` when it wrote nothing.

    **The return value is the whole point.** A handshake failure reports what
    the *socket* did — "Connection reset by peer" — which is the same sentence
    for a server that died on `execvp`, one that could not import its own
    package, and one the kernel killed. The cause is on stderr and the drain
    task has it; discarding it here meant the only way to learn why a native
    task failed was to reproduce the spawn by hand, which is not available to
    an operator reading a task error. The exit status alone is not enough
    either: bwrap exits 1 for every mount failure it has.

    Read before the drain task is cancelled, because cancelling it is what
    stops anything else arriving.
    """
    server._stopping = True
    with contextlib.suppress(Exception):
        kill_process_group(server._proc.pid)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server._proc.wait(), timeout=5)
    detail = ""
    with contextlib.suppress(Exception):
        rc = server._proc.returncode
        tail = bytes(server._stderr[-2000:]).decode("utf-8", "replace").strip()
        bits = []
        if rc is not None:
            bits.append(f"exit {rc}")
        if tail:
            bits.append(tail)
        if bits:
            detail = " (" + ": ".join(bits) + ")"
    if server._stderr_task is not None:
        server._stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server._stderr_task
    with contextlib.suppress(Exception):
        server._writer.close()
    with contextlib.suppress(Exception):
        server._sock.close()
    server._closed = True
    return detail


def build_remote_tools(server: RemoteToolServer) -> list[AgentTool]:
    """The six core tools as proxies onto ``server``.

    Same schemas and same execution modes as the real ones — the *same
    objects*, so there is no second definition to drift — which is what keeps
    the loop's parallelism and coercion behaviour identical to the in-process
    build it replaced.
    """
    tools: list[AgentTool] = []
    for schema, mode in _PROXIED:
        tools.append(AgentTool(
            schema=schema,
            execute=_make_proxy(server, schema.name),
            execution_mode=mode,
            prepare_arguments=_PREPARE.get(schema.name),
        ))
    return tools


def _make_proxy(server: RemoteToolServer, name: str):
    async def _execute(call_id, args, on_update, abort):
        return await server.call(name, str(call_id), args, on_update, abort)

    return _execute
