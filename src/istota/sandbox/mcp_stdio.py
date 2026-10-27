"""MCP over stdio: the codec a tool host speaks to the ``claude`` CLI.

Newline-delimited JSON-RPC 2.0, one message per line, the five methods the
CLI uses and nothing else. The codec knows nothing about sockets, the bridge
or the tool server: its host hands ``serve_stdio`` the tool list and two
callables, ``call(call_id, name, args)`` returning ``{content, is_error}`` in
the tool server's wire shape, and ``abort(call_id)``. In part 1 of the CLI
harness the host is ``sandbox/mcp_adapter.py``; the unified tool server (#862)
hosts this module unchanged. Stdlib only, held by ``tests/test_mcp_stdio.py``.

The method table:

==============================  ==============================================
``initialize``                  the client's protocol version if supported,
                                else the newest; capabilities ``{"tools": {}}``
``notifications/initialized``   no reply
``ping``                        ``{}``
``tools/list``                  the host's tools, ``readOnlyHint`` from
                                ``read_only``
``tools/call``                  ``call`` in its own task; ``{content, isError}``
``notifications/cancelled``     ``abort`` for that request; no reply to it
anything else                   ``-32601`` for a request, nothing for a
                                notification
==============================  ==============================================

``server/discover`` falls in the last row on purpose: the pinned CLI (2.1.296)
sends it first and falls back to ``initialize`` on ``-32601`` (Stage 1).

Concurrent calls each run as their own task keyed by the JSON-RPC id and are
answered as they finish, in any order. The host decides ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

logger = logging.getLogger("istota.sandbox.mcp_stdio")

# Newest first: an unknown client version is answered with the first.
SUPPORTED_MCP_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18")

SERVER_NAME = "istota"

# One message's line. The tool server's frame cap, since a Write's content
# crosses on one line and then in one frame.
MAX_LINE_BYTES = 32 * 1024 * 1024

_READ_CHUNK = 64 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

CallFn = Callable[[str, str, dict], Awaitable[dict]]
AbortFn = Callable[[str], Any]


def negotiate_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_MCP_VERSIONS:
        return requested
    return SUPPORTED_MCP_VERSIONS[0]


def render_tools(tools: Sequence[dict]) -> list[dict]:
    """``{name, description, input_schema, read_only}`` → MCP tool entries."""
    out: list[dict] = []
    for tool in tools:
        out.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": tool["input_schema"],
            "annotations": {"readOnlyHint": bool(tool.get("read_only"))},
        })
    return out


def content_to_mcp(blocks: Any) -> list[dict]:
    """Tool-server wire content → MCP content. An unknown block is dropped.

    Read already puts a text block naming an image before it, so nothing is
    added here.
    """
    out: list[dict] = []
    for block in blocks or ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "image":
            out.append({
                "type": "image",
                "data": str(block.get("data") or ""),
                "mimeType": str(block.get("media_type") or ""),
            })
    return out


async def _read_lines(reader: asyncio.StreamReader):
    """Yield each line's bytes, or ``None`` for a line over ``MAX_LINE_BYTES``,
    which is skipped through to its newline so the stream stays in step. A
    last line with no newline is still yielded."""
    buf = bytearray()
    skipping = False
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            if buf and not skipping:
                yield bytes(buf)
            return
        buf += chunk
        while True:
            newline = buf.find(b"\n")
            if newline < 0:
                if len(buf) > MAX_LINE_BYTES:
                    if not skipping:
                        skipping = True
                        yield None
                    buf.clear()
                break
            line = bytes(buf[:newline])
            del buf[: newline + 1]
            if skipping:
                skipping = False
                continue
            if len(line) > MAX_LINE_BYTES:
                yield None
                continue
            yield line


async def serve_stdio(
    reader: asyncio.StreamReader,
    writer: Any,
    *,
    tools: Sequence[dict],
    call: CallFn,
    abort: AbortFn,
    server_version: str,
) -> int:
    """Serve MCP on ``reader`` / ``writer`` until ``reader`` ends.

    ``writer`` needs ``write(bytes)`` and an awaitable ``drain()``. At end of
    input every call still running is aborted and cancelled: its client is
    gone. Returns 0; the host decides its own exit status.
    """
    write_lock = asyncio.Lock()
    pending: dict[str, asyncio.Task] = {}
    cancelled: set[str] = set()
    listed = render_tools(tools)

    async def send(message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with write_lock:
            writer.write(data.encode("utf-8"))
            await writer.drain()

    async def reply(request_id: Any, result: dict) -> None:
        await send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def reply_error(request_id: Any, code: int, message: str) -> None:
        await send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    async def run_call(request_id: Any, key: str, name: str, args: dict) -> None:
        try:
            outcome = await call(key, name, args)
            content = content_to_mcp(outcome.get("content") if isinstance(outcome, dict) else None)
            is_error = bool(outcome.get("is_error")) if isinstance(outcome, dict) else True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed call is a result, never a hang
            content = [{"type": "text", "text": f"istota tool call failed: {exc}"}]
            is_error = True
        finally:
            pending.pop(key, None)
        if key in cancelled:
            cancelled.discard(key)
            return
        await reply(request_id, {"content": content, "isError": is_error})

    async def handle(message: dict) -> None:
        method = message.get("method")
        is_request = "id" in message
        request_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if not isinstance(method, str):
            # A response to a request this server never sends, or junk.
            if is_request and "result" not in message and "error" not in message:
                await reply_error(request_id, INVALID_REQUEST, "Invalid Request")
            return

        if not is_request:
            if method == "notifications/cancelled":
                key = str(params.get("requestId"))
                if key in pending:
                    cancelled.add(key)
                    abort(key)
            return

        if method == "initialize":
            await reply(request_id, {
                "protocolVersion": negotiate_version(params.get("protocolVersion")),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": server_version},
            })
        elif method == "ping":
            await reply(request_id, {})
        elif method == "tools/list":
            await reply(request_id, {"tools": listed})
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                await reply_error(request_id, INVALID_PARAMS, "tools/call needs a tool name")
                return
            args = params.get("arguments")
            if not isinstance(args, dict):
                args = {}
            key = str(request_id)
            if key in pending:
                await reply_error(request_id, INVALID_REQUEST, f"request id {key} is already running")
                return
            pending[key] = asyncio.create_task(run_call(request_id, key, name, args))
        else:
            await reply_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    try:
        async for line in _read_lines(reader):
            if line is None:
                await reply_error(None, PARSE_ERROR, "Parse error")
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                await reply_error(None, PARSE_ERROR, "Parse error")
                continue
            if not isinstance(message, dict):
                await reply_error(None, INVALID_REQUEST, "Invalid Request")
                continue
            await handle(message)
    finally:
        for key, task in list(pending.items()):
            with contextlib.suppress(Exception):
                abort(key)
            task.cancel()
        for task in list(pending.values()):
            with contextlib.suppress(BaseException):
                await task
    return 0
