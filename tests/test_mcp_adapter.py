"""`sandbox/mcp_adapter.py` as a real subprocess, against an in-test bridge.

The bridge here is a plain socket server speaking the tool server's framing,
so these tests hold the adapter's half of the relay and nothing else: the
round trip, aborts from cancellation, a bridge that goes away, and the
environment the CLI hands it being gone. The bridge's own tests are
`tests/test_tool_harness.py`. Both files are deleted with the relay by the
unified tool server (#862).
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

from istota.sandbox import tool_server_protocol as proto

_TIMEOUT = 20.0

_TOOLS = [
    {"name": "Read", "description": "read a file", "input_schema": {"type": "object", "properties": {}}, "read_only": True},
    {"name": "Bash", "description": "run a command", "input_schema": {"type": "object", "properties": {}}, "read_only": False},
]


class _FakeBridge:
    """Accepts one connection, answers `hello` with `ready`, queues the rest."""

    def __init__(self):
        self.path = os.path.join(tempfile.gettempdir(), f"istota-adapter-test-{secrets.token_hex(4)}.sock")
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(1)
        self._listener.settimeout(_TIMEOUT)
        self.frames: queue.Queue = queue.Queue()
        self.conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self.conn = conn
        decoder = proto.FrameDecoder()
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            for msg in decoder.feed(data):
                if msg.get("type") == proto.MSG_HELLO:
                    conn.sendall(proto.encode({"type": proto.MSG_READY, "protocol": proto.PROTOCOL_VERSION, "tools": _TOOLS}))
                else:
                    self.frames.put(msg)

    def next_frame(self) -> dict:
        return self.frames.get(timeout=_TIMEOUT)

    def send(self, message: dict) -> None:
        assert self.conn is not None
        self.conn.sendall(proto.encode(message))

    def drop(self) -> None:
        if self.conn is not None:
            self.conn.shutdown(socket.SHUT_RDWR)
            self.conn.close()

    def close(self):
        self._listener.close()
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class _Adapter:
    """The adapter as the CLI starts it, with a line reader on its stdout."""

    def __init__(self, socket_path: str, env: dict | None = None):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "istota.sandbox.mcp_adapter", "--socket", socket_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env if env is not None else dict(os.environ),
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)

    def send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def reply(self) -> dict:
        return json.loads(self._lines.get(timeout=_TIMEOUT))

    def request(self, request_id, method, params=None) -> dict:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.reply()

    def close_stdin_and_wait(self) -> int:
        self.proc.stdin.close()
        return self.proc.wait(timeout=_TIMEOUT)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=_TIMEOUT)


@pytest.fixture
def bridge():
    fake = _FakeBridge()
    yield fake
    fake.close()


@pytest.fixture
def adapter(bridge):
    started = []

    def _start(env=None):
        proc = _Adapter(bridge.path, env=env)
        started.append(proc)
        return proc

    yield _start
    for proc in started:
        proc.kill()


def test_a_call_makes_the_round_trip(bridge, adapter):
    mcp = adapter()
    init = mcp.request(1, "initialize", {"protocolVersion": "2025-11-25"})
    assert init["result"]["serverInfo"]["name"] == "istota"

    listed = mcp.request(2, "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == ["Read", "Bash"]
    assert listed[0]["annotations"]["readOnlyHint"] is True

    mcp.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "Read", "arguments": {"file_path": "/x"}}})
    call = bridge.next_frame()
    assert call["type"] == proto.MSG_CALL
    assert call["tool"] == "Read" and call["args"] == {"file_path": "/x"}
    bridge.send({"type": proto.MSG_RESULT, "id": call["id"], "content": [
        {"type": "text", "text": "hello"},
        {"type": "image", "media_type": "image/png", "data": "QUJD", "display_name": "a.png"},
    ], "is_error": False})

    reply = mcp.reply()
    assert reply["id"] == 3
    assert reply["result"]["isError"] is False
    assert reply["result"]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "QUJD", "mimeType": "image/png"},
    ]
    assert mcp.close_stdin_and_wait() == 0


def test_cancellation_becomes_an_abort_frame_for_that_call(bridge, adapter):
    mcp = adapter()
    mcp.request(1, "initialize", {"protocolVersion": "2025-11-25"})
    mcp.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "Bash", "arguments": {"command": "sleep 30"}}})
    call = bridge.next_frame()
    assert call["type"] == proto.MSG_CALL

    mcp.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    abort = bridge.next_frame()
    assert abort == {"type": proto.MSG_ABORT, "id": call["id"]}


def test_a_bridge_that_closes_mid_call_fails_the_call_and_every_later_one(bridge, adapter):
    mcp = adapter()
    mcp.request(1, "initialize", {"protocolVersion": "2025-11-25"})
    mcp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "Bash", "arguments": {"command": "true"}}})
    bridge.next_frame()
    bridge.drop()

    pending = mcp.reply()
    assert pending["id"] == 2
    assert pending["result"]["isError"] is True
    assert pending["result"]["content"][0]["text"].startswith("istota tool bridge closed: ")

    later = mcp.request(3, "tools/call", {"name": "Read", "arguments": {}})
    assert later["result"]["isError"] is True
    assert later["result"]["content"][0]["text"].startswith("istota tool bridge closed: ")

    assert mcp.close_stdin_and_wait() != 0


def test_an_unreachable_bridge_exits_non_zero_at_once():
    missing = os.path.join(tempfile.gettempdir(), f"istota-adapter-missing-{secrets.token_hex(4)}.sock")
    proc = _Adapter(missing)
    try:
        assert proc.proc.wait(timeout=_TIMEOUT) != 0
    finally:
        proc.kill()


def _environment_of(pid: int) -> bytes:
    if sys.platform.startswith("linux"):
        with open(f"/proc/{pid}/environ", "rb") as handle:
            return handle.read()
    if sys.platform == "darwin":
        return subprocess.run(
            ["ps", "-wwE", "-p", str(pid), "-o", "command="],
            capture_output=True, check=True, timeout=_TIMEOUT,
        ).stdout
    pytest.skip("no way to read another process's environment here")


def test_the_environment_the_cli_hands_it_is_gone(bridge, adapter):
    """The CLI passes its whole env, credential included, to a stdio server.
    `os.environ.clear()` would leave the original block in place for
    `/proc/<pid>/environ`, so the adapter re-executes itself with none."""
    env = dict(os.environ)
    env["ISTOTA_ADAPTER_TEST_SECRET"] = "adapter-env-canary-value"
    mcp = adapter(env=env)
    # Answering `initialize` means the re-exec has happened.
    mcp.request(1, "initialize", {"protocolVersion": "2025-11-25"})

    seen = _environment_of(mcp.proc.pid)
    assert b"adapter-env-canary-value" not in seen
    assert b"ISTOTA_ADAPTER_TEST_SECRET" not in seen


def test_the_adapter_imports_nothing_outside_the_stdlib_and_its_two_modules():
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import istota.sandbox.mcp_adapter\n"
        "new = set(sys.modules) - before\n"
        "mods = sorted(m for m in new if m.split('.')[0] not in sys.stdlib_module_names)\n"
        "print(','.join(mods))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    loaded = {m for m in tail.split(",") if m}
    assert loaded <= {
        "istota",
        "istota.sandbox",
        "istota.sandbox.mcp_adapter",
        "istota.sandbox.mcp_stdio",
        "istota.sandbox.tool_server_protocol",
    }, loaded
