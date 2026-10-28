"""The MCP stdio server the ``claude`` CLI spawns in tool-harness mode.

``python -I -m istota.sandbox.mcp_adapter --socket PATH``, started by the CLI
from the harness's ``--mcp-config`` file. It hosts ``sandbox.mcp_stdio`` on its
stdin and stdout and relays every call to the daemon's bridge over the Unix
socket at ``PATH``, in the tool server's own framing
(``sandbox/tool_server_protocol.py``): ``hello`` then ``ready`` (the tool
list), then ``call`` / ``abort`` / ``shutdown`` down and ``result`` / ``fatal``
up. It has no policy of its own; the bridge decides what runs and when.

**The environment is dropped first**, by re-executing itself with an empty
one. The CLI passes its whole env to a stdio server whatever the config's
``env`` says: its credential, its messaging socket and token, its config dir
(Stage 1). ``-I`` keeps the cwd and the user site off ``sys.path``.

**Nothing hangs.** If the socket closes, every pending and later call is
answered ``isError`` with ``istota tool bridge closed: <reason>``, and the
process exits non-zero once stdin closes. A bridge it cannot reach or that
does not answer ``hello`` exits non-zero at once, which the CLI reports as a
failed server.

Interim: the unified tool server (#862) speaks MCP itself and this module is
deleted with the bridge. Imports nothing outside the stdlib,
``istota.sandbox.tool_server_protocol`` and ``istota.sandbox.mcp_stdio``, held
by ``tests/test_mcp_adapter.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys

from istota.sandbox import tool_server_protocol as proto
from istota.sandbox.mcp_stdio import serve_stdio, stdio_streams

_CLOSED_PREFIX = "istota tool bridge closed: "
_EXIT_BRIDGE_CLOSED = 1
_EXIT_NO_BRIDGE = 2


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version("istota")
    except Exception:  # noqa: BLE001 — a missing dist-info is not a failure
        return "0"


class _Relay:
    """The socket half: pending calls by id, and the reason it closed."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._decoder = proto.FrameDecoder()
        self.closed: str | None = None
        self.tools: list[dict] = []

    async def send(self, message: dict) -> None:
        async with self._write_lock:
            self._writer.write(proto.encode(message))
            await self._writer.drain()

    async def handshake(self) -> None:
        await self.send({"type": proto.MSG_HELLO, "protocol": proto.PROTOCOL_VERSION})
        while True:
            data = await self._reader.read(65536)
            if not data:
                raise ConnectionError("the bridge closed before it was ready")
            for msg in self._decoder.feed(data):
                kind = msg.get("type")
                if kind == proto.MSG_READY:
                    tools = msg.get("tools")
                    self.tools = [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
                    return
                if kind == proto.MSG_FATAL:
                    raise ConnectionError(f"the bridge refused: {msg.get('message')}")
                raise ConnectionError(f"the bridge sent {kind!r} before it was ready")

    def close(self, reason: str) -> None:
        if self.closed is not None:
            return
        self.closed = reason
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(_closed_result(reason))
        self._pending.clear()

    async def read_frames(self) -> None:
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    self.close("the connection ended")
                    return
                for msg in self._decoder.feed(data):
                    kind = msg.get("type")
                    if kind == proto.MSG_RESULT:
                        fut = self._pending.pop(str(msg.get("id")), None)
                        if fut is not None and not fut.done():
                            fut.set_result({
                                "content": msg.get("content") or [],
                                "is_error": bool(msg.get("is_error")),
                            })
                    elif kind == proto.MSG_FATAL:
                        self.close(str(msg.get("message") or "fatal"))
                        return
                    # `update` frames are dropped: the CLI is not told about
                    # partial output.
        except asyncio.CancelledError:
            raise
        except proto.ProtocolError as exc:
            self.close(f"bad frame: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.close(str(exc) or type(exc).__name__)

    async def call(self, call_id: str, name: str, args: dict) -> dict:
        if self.closed is not None:
            return _closed_result(self.closed)
        fut = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self.send({"type": proto.MSG_CALL, "id": call_id, "tool": name, "args": args})
        except proto.ProtocolError as exc:
            self._pending.pop(call_id, None)
            return {"content": [{"type": "text", "text": f"Tool call could not be sent: {exc}"}], "is_error": True}
        except Exception as exc:  # noqa: BLE001
            self.close(str(exc) or type(exc).__name__)
            return _closed_result(self.closed or "")
        try:
            return await fut
        finally:
            self._pending.pop(call_id, None)

    def abort(self, call_id: str) -> None:
        if self.closed is not None:
            return
        asyncio.ensure_future(self._send_abort(call_id))

    async def _send_abort(self, call_id: str) -> None:
        with contextlib.suppress(Exception):
            await self.send({"type": proto.MSG_ABORT, "id": call_id})

    async def shutdown(self) -> None:
        if self.closed is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send({"type": proto.MSG_SHUTDOWN}), timeout=1.0)
        with contextlib.suppress(Exception):
            self._writer.close()


def _closed_result(reason: str) -> dict:
    return {"content": [{"type": "text", "text": _CLOSED_PREFIX + reason}], "is_error": True}


async def _run(socket_path: str) -> int:
    try:
        sock_reader, sock_writer = await asyncio.open_unix_connection(socket_path)
    except OSError as exc:
        print(f"istota mcp adapter: cannot reach the bridge: {exc.strerror or exc}", file=sys.stderr)
        return _EXIT_NO_BRIDGE
    relay = _Relay(sock_reader, sock_writer)
    try:
        await asyncio.wait_for(relay.handshake(), timeout=proto.STARTUP_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        print(f"istota mcp adapter: no handshake with the bridge: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            sock_writer.close()
        return _EXIT_NO_BRIDGE

    frames = asyncio.ensure_future(relay.read_frames())
    stdin, stdout = await stdio_streams(sys.stdin.buffer, sys.stdout.buffer)
    try:
        await serve_stdio(
            stdin, stdout,
            tools=relay.tools,
            call=relay.call,
            abort=relay.abort,
            server_version=_server_version(),
        )
    finally:
        frames.cancel()
        with contextlib.suppress(BaseException):
            await frames
        await relay.shutdown()
    return 0 if relay.closed is None else _EXIT_BRIDGE_CLOSED


def _reexec_without_env(argv: list[str]) -> None:
    """Replace this process with itself under an empty environment.

    ``os.environ.clear()`` alone is not enough: it unsets the variables, but
    the block the kernel shows as ``/proc/<pid>/environ`` is the one the
    process was started with, and it keeps the CLI's credential. ``--clean-env``
    marks the second run, so this happens at most once.
    """
    os.execve(
        sys.executable,
        [sys.executable, "-I", "-m", "istota.sandbox.mcp_adapter", *argv, "--clean-env"],
        {},
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="istota.sandbox.mcp_adapter")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--clean-env", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.clean_env:
        _reexec_without_env(argv)
    os.environ.clear()
    return asyncio.run(_run(args.socket))


if __name__ == "__main__":
    sys.exit(main())
