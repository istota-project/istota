"""The MCP stdio codec, over in-memory streams with stub `call` / `abort`.

Nothing here knows about the bridge socket or the tool server: the codec's
host supplies `call` and `abort`, and these tests supply stubs. That is what
lets the unified tool server (#862) host the same module and keep this file.
"""

import asyncio
import json
import subprocess
import sys

import pytest

from istota.sandbox import mcp_stdio

TOOLS = [
    {"name": "Read", "description": "read a file", "input_schema": {"type": "object", "properties": {}, "required": []}, "read_only": True},
    {"name": "Bash", "description": "run a command", "input_schema": {"type": "object", "properties": {}, "required": []}, "read_only": False},
]


class _Writer:
    """What `serve_stdio` writes to: `write` plus an awaitable `drain`."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        return None

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.buf.decode().splitlines() if line]


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


async def _until_quiet(reader, writer):
    """Keep input open until the output stops growing: end of input aborts
    whatever is still running, which is its own test below."""
    size, still = -1, 0
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(writer.buf) == size:
            still += 1
            if still >= 5:
                return
        else:
            size, still = len(writer.buf), 0


async def _serve(lines, *, call=None, abort=None, tools=TOOLS, feed=None):
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    writer = _Writer()

    async def _default_call(call_id, name, args):
        return {"content": [{"type": "text", "text": f"{name}:{call_id}"}], "is_error": False}

    if feed is None:
        feed = _until_quiet

    task = asyncio.create_task(mcp_stdio.serve_stdio(
        reader, writer,
        tools=tools,
        call=call or _default_call,
        abort=abort or (lambda call_id: None),
        server_version="9.9.9",
    ))
    await feed(reader, writer)
    reader.feed_eof()
    code = await asyncio.wait_for(task, 5)
    return code, writer.messages()


def _init(version, id_=1):
    return _line({"jsonrpc": "2.0", "id": id_, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"},
    }})


class TestInitialize:
    @pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18"])
    async def test_a_supported_version_is_echoed(self, version):
        code, out = await _serve([_init(version)])
        assert code == 0
        result = out[0]["result"]
        assert out[0]["id"] == 1
        assert result["protocolVersion"] == version
        assert result["capabilities"] == {"tools": {}}
        assert result["serverInfo"] == {"name": "istota", "version": "9.9.9"}

    async def test_an_unknown_version_is_answered_with_the_newest(self):
        _, out = await _serve([_init("1999-01-01")])
        assert out[0]["result"]["protocolVersion"] == mcp_stdio.SUPPORTED_MCP_VERSIONS[0]
        assert mcp_stdio.SUPPORTED_MCP_VERSIONS[0] == "2025-11-25"

    async def test_server_discover_is_method_not_found_so_the_cli_falls_back(self):
        # 2.1.296 sends `server/discover` first and falls back to `initialize`
        # on -32601 (Stage 1).
        _, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 0, "method": "server/discover",
            "params": {"_meta": {"protocolVersion": "2026-07-28"}},
        })])
        assert out == [{"jsonrpc": "2.0", "id": 0, "error": {"code": -32601, "message": "Method not found: server/discover"}}]

    async def test_initialized_gets_no_reply_and_ping_gets_an_empty_result(self):
        _, out = await _serve([
            _line({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            _line({"jsonrpc": "2.0", "id": 7, "method": "ping"}),
        ])
        assert out == [{"jsonrpc": "2.0", "id": 7, "result": {}}]


class TestToolsList:
    async def test_tools_are_listed_with_the_read_only_hint(self):
        _, out = await _serve([_line({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})])
        tools = out[0]["result"]["tools"]
        assert [t["name"] for t in tools] == ["Read", "Bash"]
        assert tools[0] == {
            "name": "Read",
            "description": "read a file",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": {"readOnlyHint": True},
        }
        assert tools[1]["annotations"] == {"readOnlyHint": False}


class TestToolsCall:
    async def test_text_content_maps_to_mcp_text(self):
        seen = []

        async def call(call_id, name, args):
            seen.append((call_id, name, args))
            return {"content": [{"type": "text", "text": "hello"}], "is_error": False}

        _, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "Read", "arguments": {"file_path": "/x"}},
        })], call=call)
        assert seen == [("5", "Read", {"file_path": "/x"})]
        assert out == [{"jsonrpc": "2.0", "id": 5, "result": {
            "content": [{"type": "text", "text": "hello"}], "isError": False,
        }}]

    async def test_image_content_maps_to_mcp_image_and_error_is_carried(self):
        async def call(call_id, name, args):
            return {"content": [
                {"type": "text", "text": "image: a.png"},
                {"type": "image", "media_type": "image/png", "data": "QUJD", "display_name": "a.png"},
                {"type": "something-else"},
            ], "is_error": True}

        _, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "Read", "arguments": {}},
        })], call=call)
        assert out[0]["result"] == {
            "content": [
                {"type": "text", "text": "image: a.png"},
                {"type": "image", "data": "QUJD", "mimeType": "image/png"},
            ],
            "isError": True,
        }

    async def test_a_call_that_raises_is_an_error_result_not_a_hang(self):
        async def call(call_id, name, args):
            raise RuntimeError("bridge closed")

        _, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "Bash", "arguments": {"command": "true"}},
        })], call=call)
        assert out[0]["result"]["isError"] is True
        assert "bridge closed" in out[0]["result"]["content"][0]["text"]

    async def test_missing_arguments_default_to_an_empty_object(self):
        seen = []

        async def call(call_id, name, args):
            seen.append(args)
            return {"content": [], "is_error": False}

        await _serve([_line({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "Read"},
        })], call=call)
        assert seen == [{}]

    async def test_a_call_with_no_name_is_invalid_params(self):
        _, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"arguments": {}},
        })])
        assert out[0]["error"]["code"] == -32602

    async def test_concurrent_calls_are_answered_by_id_out_of_order(self):
        release_first = asyncio.Event()
        started = []

        async def call(call_id, name, args):
            started.append(call_id)
            if call_id == "1":
                await release_first.wait()
            return {"content": [{"type": "text", "text": call_id}], "is_error": False}

        async def feed(reader, writer):
            reader.feed_data(_line({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "Read", "arguments": {}}}))
            reader.feed_data(_line({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "Read", "arguments": {}}}))
            for _ in range(200):
                if writer.messages():
                    break
                await asyncio.sleep(0.01)
            # The second answer is out while the first call is still held.
            assert [m["id"] for m in writer.messages()] == [2]
            assert started == ["1", "2"]
            release_first.set()
            for _ in range(200):
                if len(writer.messages()) == 2:
                    break
                await asyncio.sleep(0.01)

        _, out = await _serve([], call=call, feed=feed)
        assert [m["id"] for m in out] == [2, 1]
        assert out[1]["result"]["content"][0]["text"] == "1"

    async def test_cancelled_calls_abort_with_the_right_id(self):
        aborted = []
        gate = asyncio.Event()

        async def call(call_id, name, args):
            await gate.wait()
            return {"content": [{"type": "text", "text": "late"}], "is_error": False}

        def abort(call_id):
            aborted.append(call_id)
            gate.set()

        async def feed(reader, writer):
            reader.feed_data(_line({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "Bash", "arguments": {}}}))
            reader.feed_data(_line({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "Bash", "arguments": {}}}))
            await asyncio.sleep(0.05)
            reader.feed_data(_line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4, "reason": "user"}}))
            # A cancel for an id that is not pending is ignored.
            reader.feed_data(_line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}}))
            for _ in range(200):
                if aborted:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

        _, out = await _serve([], call=call, abort=abort, feed=feed)
        assert aborted == ["4"]
        # The cancelled request gets no response; the other one finishes once
        # the shared gate opens.
        assert [m["id"] for m in out] == [3]

    async def test_end_of_input_aborts_pending_calls_and_returns(self):
        aborted = []

        async def call(call_id, name, args):
            await asyncio.Event().wait()

        code, out = await _serve([_line({
            "jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "Bash", "arguments": {}},
        })], call=call, abort=aborted.append)
        assert code == 0
        assert aborted == ["11"]
        assert out == []

    async def test_a_duplicate_pending_id_is_refused(self):
        gate = asyncio.Event()

        async def call(call_id, name, args):
            await gate.wait()
            return {"content": [], "is_error": False}

        async def feed(reader, writer):
            msg = _line({"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "Read", "arguments": {}}})
            reader.feed_data(msg)
            reader.feed_data(msg)
            for _ in range(200):
                if writer.messages():
                    break
                await asyncio.sleep(0.01)
            gate.set()
            await asyncio.sleep(0.05)

        _, out = await _serve([], call=call, feed=feed)
        assert out[0]["error"]["code"] == -32600
        assert out[1]["result"] == {"content": [], "isError": False}


class TestErrors:
    async def test_an_unknown_method_is_minus_32601(self):
        _, out = await _serve([_line({"jsonrpc": "2.0", "id": "a", "method": "resources/list"})])
        assert out[0]["id"] == "a"
        assert out[0]["error"]["code"] == -32601

    async def test_an_unknown_notification_gets_no_reply(self):
        _, out = await _serve([_line({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})])
        assert out == []

    async def test_a_line_that_is_not_json_is_minus_32700_with_a_null_id(self):
        _, out = await _serve([b"{not json\n", _line({"jsonrpc": "2.0", "id": 1, "method": "ping"})])
        assert out[0] == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        # The stream goes on after a bad line.
        assert out[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}

    async def test_a_json_value_that_is_not_an_object_is_invalid_request(self):
        _, out = await _serve([b"[1, 2]\n"])
        assert out[0]["error"]["code"] == -32600
        assert out[0]["id"] is None

    async def test_an_overlong_line_is_refused_and_the_stream_recovers(self, monkeypatch):
        monkeypatch.setattr(mcp_stdio, "MAX_LINE_BYTES", 64)
        big = _line({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 200}})
        _, out = await _serve([big[:50], big[50:], _line({"jsonrpc": "2.0", "id": 2, "method": "ping"})])
        assert out[0]["error"]["code"] == -32700
        assert out[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}

    async def test_a_final_line_without_a_newline_is_still_read(self):
        _, out = await _serve([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()])
        assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    async def test_output_is_one_message_per_line(self):
        async def call(call_id, name, args):
            return {"content": [{"type": "text", "text": "a\nb\nc"}], "is_error": False}

        writer = _Writer()

        async def feed(reader, w):
            reader.feed_data(_line({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "Read", "arguments": {}}}))
            await _until_quiet(reader, w)
            writer.buf = w.buf

        _, out = await _serve([], call=call, feed=feed)
        assert out[0]["result"]["content"][0]["text"] == "a\nb\nc"
        assert writer.buf.count(b"\n") == 1


def test_the_codec_imports_nothing_outside_the_stdlib():
    """The unified tool server hosts this module inside the sandbox, and the
    part 1 adapter runs it under `python -I`; neither may pull in a third-party
    package or the rest of istota."""
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import istota.sandbox.mcp_stdio\n"
        "new = set(sys.modules) - before\n"
        "mods = sorted(m for m in new if m.split('.')[0] not in sys.stdlib_module_names)\n"
        "print(','.join(mods))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    loaded = {m for m in tail.split(",") if m}
    assert loaded <= {"istota", "istota.sandbox", "istota.sandbox.mcp_stdio"}, loaded
