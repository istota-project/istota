"""The native brain's tool server: one per task attempt, inside the sandbox.

``python -m istota.tool_server --fd N``. The daemon spawns it once through
``build_bwrap_cmd(..., profile=NATIVE)``, places it in the task cgroup from
``preexec_fn``, and talks to it over the inherited socketpair end on fd ``N``.
It builds one ``ToolEnv`` from the ``hello`` frame, binds the six core tools to
it, and answers ``call`` frames until it is told to stop. See
``session/tools/remote.py`` for the daemon's half and for why the transport is
an inherited descriptor.

Three things about the environment it runs in are worth stating here, because
each is a decision rather than a consequence.

**It never wraps anything in bubblewrap.** It is already inside the one
namespace the attempt gets. A nested wrap would not merely be redundant: the
outer sandbox passes ``--unshare-user --disable-userns`` where bwrap supports
them, which switches off the nested user namespace a second bwrap needs, so
every ``Bash`` call would fail rather than anything being weakened. The
``ToolEnv`` it builds therefore carries no wrap, and ``bash.py`` no longer has
a field to read one from.

**It never places anything in a cgroup.** Membership is inherited at ``fork``,
so every command it runs is already in the task cgroup by being forked from
here — and ``cgroup.procs`` is under a path no sandbox binds, so trying would
fail anyway.

**It merges the proxy variables into the environment it hands its children.**
``build_bwrap_cmd``'s network-bridge wrapper starts the bridge and execs ``env
HTTPS_PROXY=… HTTP_PROXY=… NO_PROXY= "$@"``, so those land in *this* process's
environment, while the ``subprocess_env`` in the ``hello`` frame was built by
the daemon, which has no bridge and no port. Without the merge every
network-using build inside a task would fail with an error pointing nowhere
near this file. ``NO_PROXY`` is deliberately the **empty string** in that
wrapper — it blanks an inherited exemption list — so the merge tests presence,
never truthiness, and the wrapper's value wins a collision because inside
``--unshare-net`` the bridge on loopback is the only proxy anything can reach.

Nothing here raises past ``main``. A failure at any point sends ``fatal`` and
exits non-zero, which the daemon turns into a failed attempt; frame *contents*
are never logged, because they carry the model's tool arguments and the tool
output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Any

from istota import tool_server_protocol as proto

# Set on this process by `build_bwrap_cmd`'s bridge wrapper, needed by the
# children this process forks. `NO_PROXY=` is set to empty on purpose there, so
# membership is what counts and a falsy value must still be carried.
_PROXY_ENV_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


def merge_proxy_env(
    subprocess_env: dict[str, str] | None,
    process_env: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Fold this process's proxy variables into the env for Bash children.

    ``None`` in and nothing to merge → ``None`` out, which is ``ToolEnv``'s
    "inherit the parent environment" and therefore already correct. ``None`` in
    with proxy variables present becomes a dict of *this* process's whole
    environment plus them, since anything less would silently narrow what a
    direct (non-task) caller's Bash children inherit.

    **This process's value wins a collision, not the daemon's**, and the
    asymmetry is about reachability rather than about precedence in the
    abstract. The only thing that puts a proxy variable in *this* environment
    is ``build_bwrap_cmd``'s bridge wrapper, which ``exec env``s it onto the
    process bwrap runs; and inside ``--unshare-net`` the bridge on loopback is
    the only proxy any child can reach. A daemon-side value — an operator's
    ``passthrough_env_vars = ["HTTPS_PROXY"]`` naming a corporate proxy, say —
    is a host address with no route from in here, so preferring it would point
    every build at something unreachable and fail it with an error naming
    neither this file nor the wrapper. Where there is no bridge the wrapper set
    nothing, ``found`` is empty, and the daemon's value passes through
    untouched, which is the case that setting was written for.
    """
    env = os.environ if process_env is None else process_env
    found = {k: env[k] for k in _PROXY_ENV_VARS if k in env}
    if not found:
        return subprocess_env
    if subprocess_env is None:
        merged = dict(env)
    else:
        merged = dict(subprocess_env)
    merged.update(found)
    return merged


def build_env(hello: dict[str, Any], process_env: dict[str, str] | None = None):
    """The ``hello`` frame → the ``ToolEnv`` the six tools are bound to.

    Importable on its own, and tested that way: it is the whole translation
    between what the daemon knows about a task and what the tools enforce, and
    a test that asserts a request's roots reach the tools should not need a
    subprocess to say so.
    """
    from istota.session.tools import ToolEnv

    def _roots(key: str) -> tuple[Path, ...] | None:
        value = hello.get(key)
        if value is None:
            return None
        return tuple(Path(p) for p in value)

    # Read off a default `ToolEnv` rather than restated as literals here. The
    # daemon's `_hello_payload` takes them from the same place and says why:
    # a second set of numbers is a silent second default the day one of them
    # changes. `is None` rather than `or`, because `0` is a legitimate value
    # for the two caps that read it as "no cap".
    defaults = ToolEnv(cwd=Path(hello["cwd"]))

    def _cap(key: str, fallback: int) -> int:
        value = hello.get(key)
        return fallback if value is None else int(value)

    deferred = hello.get("deferred_dir")
    return ToolEnv(
        cwd=Path(hello["cwd"]),
        subprocess_env=merge_proxy_env(hello.get("subprocess_env"), process_env),
        bash_timeout_seconds=_cap("bash_timeout_seconds", defaults.bash_timeout_seconds),
        max_output_bytes=_cap("max_output_bytes", defaults.max_output_bytes),
        max_read_lines=_cap("max_read_lines", defaults.max_read_lines),
        max_read_bytes=_cap("max_read_bytes", defaults.max_read_bytes),
        read_roots=_roots("read_roots"),
        write_roots=_roots("write_roots"),
        write_denied_roots=tuple(_roots("write_denied_roots") or ()),
        deferred_dir=Path(deferred) if deferred else None,
        bash_spill_full_output=bool(hello.get("bash_spill_full_output", True)),
        # WebFetch stays in the daemon: it makes a network request with no
        # credentials and its whole hardening is about resolved IPs, none of
        # which a namespace helps with. `build_default_tools` omits it when
        # this is None, which is what keeps the server at exactly six tools.
        web_fetch=None,
    )


class _Server:
    """One connection, its tools, and the calls running on it."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._tools: dict[str, Any] = {}
        self._aborts: dict[str, asyncio.Event] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    async def _send(self, message: dict) -> None:
        assert self._writer is not None
        async with self._write_lock:
            self._writer.write(proto.encode(message))
            await self._writer.drain()

    async def run(self) -> int:
        self._reader, self._writer = await asyncio.open_connection(sock=self._sock)
        decoder = proto.FrameDecoder()
        try:
            while not self._stop.is_set():
                data = await self._reader.read(65536)
                if not data:
                    # The daemon went away. Its own kill paths cover the
                    # process tree; exiting here is what makes `--unshare-pid`
                    # take the rest of the namespace with us.
                    decoder.close()
                    return 0
                for message in decoder.feed(data):
                    await self._handle(message)
        except proto.ProtocolError as exc:
            await self._fatal(f"protocol error: {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001 — nothing may escape to a traceback
            await self._fatal(f"{type(exc).__name__}: {exc}")
            return 2
        finally:
            await self._drain_running()
        return 0

    async def _fatal(self, message: str) -> None:
        # Best-effort: the socket may already be gone, which is often *why*
        # this is being sent.
        try:
            await self._send({"type": proto.MSG_FATAL, "message": message})
        except Exception:  # noqa: BLE001
            pass

    async def _drain_running(self) -> None:
        for task in list(self._running.values()):
            task.cancel()
        for task in list(self._running.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._running.clear()

    async def _handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == proto.MSG_HELLO:
            await self._on_hello(message)
            return
        if kind == proto.MSG_CALL:
            if not self._tools:
                # The `hello`-first rule, which was enforced nowhere: with no
                # tools bound every name resolves to None and the caller was
                # told "Unknown tool: Read", which sends the reader looking at
                # the tool table rather than at the handshake.
                raise proto.ProtocolError("a call arrived before hello")
            self._on_call(message)
            return
        if kind == proto.MSG_ABORT:
            event = self._aborts.get(str(message.get("id")))
            if event is not None:
                event.set()
            return
        if kind == proto.MSG_SHUTDOWN:
            self._stop.set()
            return
        raise proto.ProtocolError(f"the daemon sent {kind!r}, which a server does not take")

    async def _on_hello(self, message: dict) -> None:
        if self._tools:
            raise proto.ProtocolError("a second hello frame")
        got = message.get("protocol")
        if got not in proto.SUPPORTED_PROTOCOLS:
            raise proto.ProtocolError(
                f"the daemon speaks protocol {got!r}, this server speaks "
                f"{sorted(proto.SUPPORTED_PROTOCOLS)}"
            )
        from istota.session.tools import build_default_tools

        env = build_env(message)
        self._tools = {t.schema.name: t for t in build_default_tools(env)}
        await self._send({
            "type": proto.MSG_READY,
            "protocol": proto.PROTOCOL_VERSION,
            "tools": sorted(self._tools),
        })

    def _on_call(self, message: dict) -> None:
        call_id = str(message.get("id"))
        # The abort event is registered **here**, synchronously, not inside the
        # coroutine below. `decoder.feed` returns a list, so an `abort` frame
        # arriving in the same read as its `call` is handled on the next line
        # with no await in between — and `_handle` drops an abort for an id it
        # cannot find. Registering in the coroutine leaves that window open,
        # and the daemon closes it routinely: `call()` starts its abort watcher
        # straight after sending, so an abort already set (the loop's own,
        # which the failure path sets) produces exactly that frame ordering.
        abort = asyncio.Event()
        self._aborts[call_id] = abort
        # A task per call rather than an await: the daemon's loop dispatches a
        # whole batch of parallel-mode tools at once, and serializing them here
        # would silently undo `execution_mode="parallel"` for every one of
        # them while every schema assertion stayed green.
        self._running[call_id] = asyncio.ensure_future(
            self._run_call(call_id, message, abort)
        )

    async def _run_call(self, call_id: str, message: dict, abort: asyncio.Event) -> None:
        name = str(message.get("tool"))
        args = message.get("args")
        try:
            tool = self._tools.get(name)
            if tool is None:
                await self._result(call_id, _error(f"Unknown tool: {name}"))
                return
            if not isinstance(args, dict):
                await self._result(call_id, _error(f"{name}: arguments must be an object"))
                return

            async def _on_update(text: str) -> None:
                await self._send({"type": proto.MSG_UPDATE, "id": call_id, "text": text})

            try:
                result = await tool.execute(call_id, args, _on_update, abort)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # The same contract every tool already satisfies: a failure is
                # an error result, never something that reaches the loop.
                result = _error(f"{name} failed: {type(exc).__name__}: {exc}")
            await self._result(call_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fatal(f"tool dispatch failed: {type(exc).__name__}: {exc}")
        finally:
            self._aborts.pop(call_id, None)
            self._running.pop(call_id, None)

    async def _result(self, call_id: str, result) -> None:
        """Send one `result` frame, or an error result if it will not fit.

        The codec refuses a frame over `MAX_FRAME_BYTES`, and that refusal must
        be **this call's** problem rather than the attempt's: falling through
        to the outer handler would send `fatal` and fail the whole task over
        one oversized `Grep` in content mode. The daemon's `call()` already
        handles the mirror case this way and says so — "the server is healthy;
        this call is not" — and the asymmetry was the bug.
        """
        from istota.session.tools.remote import content_to_wire

        frame = {
            "type": proto.MSG_RESULT,
            "id": call_id,
            "content": content_to_wire(result.content),
            "is_error": bool(result.is_error),
            "terminate": bool(result.terminate),
        }
        try:
            await self._send(frame)
            return
        except proto.ProtocolError as exc:
            replacement = _error(f"The result could not be returned: {exc}")
        await self._send({
            "type": proto.MSG_RESULT,
            "id": call_id,
            "content": content_to_wire(replacement.content),
            "is_error": True,
            "terminate": False,
        })


def _error(text: str):
    from istota.agent.tools import ToolResult
    from istota.llm.types import TextContent

    return ToolResult(content=[TextContent(text=text)], is_error=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="istota.tool_server", add_help=True)
    parser.add_argument(
        "--fd",
        type=int,
        required=True,
        help="inherited socketpair descriptor to serve on",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        sock = socket.socket(fileno=args.fd)
    except OSError as exc:
        # Before any socket exists, so there is nowhere to send `fatal`.
        # stderr is drained by the daemon and shown in the failure message.
        print(f"tool server: fd {args.fd} is not usable: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_Server(sock).run())
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
