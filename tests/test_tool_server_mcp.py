"""The tool server's MCP front end, ``--mcp --hello PATH``, as a real subprocess.

Unwrapped, as `tests/test_tool_server.py` runs the ``--fd`` front end: the
containment claims belong to the linux and image tiers. What is held here is
the front end the ``claude`` CLI will spawn (#862): the handshake, the tool
list rendered from the server's own tools, calls through the server's
dispatcher, cancellation, the call gate, stdin EOF, and a stdout that carries
nothing but JSON-RPC.
"""

import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

import pytest

from istota.session.tools import ToolEnv, hello_payload

_TIMEOUT = 20.0
_QUIET = 0.6


def _hello(workspace, **kw):
    env = ToolEnv(cwd=workspace)
    extra = {}
    for key in ("gate",):
        if key in kw:
            extra[key] = kw.pop(key)
    args = dict(
        cwd=workspace,
        subprocess_env=None,
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=None,
        bash_timeout_seconds=30,
        max_output_bytes=env.max_output_bytes,
        max_read_lines=env.max_read_lines,
        max_read_bytes=env.max_read_bytes,
        bash_spill_full_output=False,
    )
    args.update(kw)
    hello = hello_payload(**args)
    hello.update(extra)
    return hello


def _write_hello(tmp_path, hello, name="tool-hello.json"):
    path = tmp_path / name
    path.write_text(json.dumps(hello))
    os.chmod(path, 0o600)
    return path


def _argv(hello_path):
    return [sys.executable, "-I", "-m", "istota.sandbox.tool_server", "--mcp", "--hello", str(hello_path)]


class _Mcp:
    """The server as the CLI starts it: stdio, a line reader on stdout."""

    def __init__(self, hello_path, *, over_socket=False):
        self._sock = None
        if over_socket:
            # The pinned CLI hands a stdio server Unix sockets, not pipes
            # (Stage 1's probe): one socketpair per descriptor, as libuv
            # makes them, so one test serves over that.
            in_ours, in_theirs = socket.socketpair()
            out_ours, out_theirs = socket.socketpair()
            self.proc = subprocess.Popen(
                _argv(hello_path), stdin=in_theirs, stdout=out_theirs, stderr=subprocess.PIPE,
            )
            in_theirs.close()
            out_theirs.close()
            self._sock = in_ours
            self._out_sock = out_ours
            self._out = out_ours.makefile("rb")
            self._in = in_ours.makefile("wb")
        else:
            self.proc = subprocess.Popen(
                _argv(hello_path),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._out = self.proc.stdout
            self._in = self.proc.stdin
        self.raw_lines: list[bytes] = []
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self._out:
                self.raw_lines.append(line)
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def send(self, message: dict) -> None:
        self._in.write((json.dumps(message) + "\n").encode())
        self._in.flush()

    def reply(self, timeout=_TIMEOUT) -> dict:
        line = self._lines.get(timeout=timeout)
        assert line is not None, "the server closed its stdout"
        return json.loads(line)

    def no_reply_within(self, seconds) -> bool:
        try:
            self._lines.get(timeout=seconds)
        except queue.Empty:
            return True
        return False

    def request(self, request_id, method, params=None) -> dict:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.reply()

    def call(self, request_id, name, arguments) -> dict:
        return self.request(request_id, "tools/call", {"name": name, "arguments": arguments})

    def initialize(self) -> dict:
        return self.request(0, "initialize", {"protocolVersion": "2025-11-25"})

    def close_stdin_and_wait(self) -> int:
        if self._sock is not None:
            self._sock.shutdown(socket.SHUT_WR)
        else:
            self._in.close()
        return self.proc.wait(timeout=_TIMEOUT)

    def stderr(self) -> str:
        return self.proc.stderr.read().decode("utf-8", "replace")

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=_TIMEOUT)
        if self._sock is not None:
            self._sock.close()
            self._out_sock.close()


@pytest.fixture
def server(tmp_path):
    started = []

    def _start(hello=None, *, over_socket=False, **kw):
        hello_path = _write_hello(tmp_path, hello if hello is not None else _hello(tmp_path, **kw))
        proc = _Mcp(hello_path, over_socket=over_socket)
        started.append(proc)
        return proc

    yield _start
    for proc in started:
        proc.kill()


def _text(reply):
    return "".join(b.get("text", "") for b in reply["result"]["content"] if b.get("type") == "text")


def _open_gate_writer(path):
    deadline = time.monotonic() + _TIMEOUT
    while True:
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            if time.monotonic() > deadline:
                raise AssertionError("the server never opened the gate for reading")
            time.sleep(0.02)


class TestHandshake:
    def test_initialize_names_the_server(self, server):
        mcp = server()
        init = mcp.initialize()
        assert init["result"]["serverInfo"]["name"] == "istota"
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert mcp.close_stdin_and_wait() == 0

    def test_server_discover_is_method_not_found_not_a_close(self, server):
        """The pinned CLI respawns a server that closes on `server/discover`,
        which would be a second pid report under one CLI (Stage 1)."""
        mcp = server()
        reply = mcp.request(1, "server/discover")
        assert reply["error"]["code"] == -32601
        assert mcp.initialize()["result"]["serverInfo"]["name"] == "istota"

    def test_tools_list_is_the_servers_own_tools(self, server):
        mcp = server()
        mcp.initialize()
        listed = {t["name"]: t for t in mcp.request(1, "tools/list")["result"]["tools"]}
        assert set(listed) == {
            "Bash", "BashOutput", "Edit", "Glob", "Grep", "KillShell", "Read", "Write",
        }
        read_only = {name for name, t in listed.items() if t["annotations"]["readOnlyHint"]}
        assert read_only == {"Read", "Grep", "Glob", "BashOutput"}
        assert "exclude_from_context" not in listed["Bash"]["inputSchema"]["properties"]
        assert "run_in_background" in listed["Bash"]["inputSchema"]["properties"]
        assert "command" in listed["Bash"]["inputSchema"]["properties"]
        assert listed["Read"]["description"]

    def test_a_background_job_answers_over_mcp_and_ends_with_stdin(self, server, tmp_path):
        marker = tmp_path / "beat"
        mcp = server()
        mcp.initialize()
        started = mcp.call(1, "Bash", {
            "command": f"echo hi; while :; do date +%s%N >> {marker}; sleep 0.05; done",
            "run_in_background": True,
        })
        assert "bg_1" in _text(started)
        read = mcp.call(2, "BashOutput", {"job_id": "bg_1", "wait_seconds": 1})
        assert "hi" in _text(read) and "running" in _text(read)
        assert mcp.close_stdin_and_wait() == 0
        size = marker.stat().st_size
        time.sleep(0.4)
        assert marker.stat().st_size == size

    def test_the_hellos_subset_is_what_is_listed_and_callable(self, server, tmp_path):
        mcp = server(tools=["Read"])
        mcp.initialize()
        listed = mcp.request(1, "tools/list")["result"]["tools"]
        assert [t["name"] for t in listed] == ["Read"]
        refused = mcp.call(2, "Write", {"file_path": str(tmp_path / "x"), "content": "x"})
        assert refused["result"]["isError"] is True
        assert "Unknown tool: Write" in _text(refused)
        assert not (tmp_path / "x").exists()


class TestCalls:
    def test_a_read_round_trip(self, server, tmp_path):
        (tmp_path / "note.txt").write_text("alpha\nbeta\n")
        mcp = server()
        mcp.initialize()
        reply = mcp.call(1, "Read", {"file_path": str(tmp_path / "note.txt")})
        assert reply["result"]["isError"] is False
        assert "beta" in _text(reply)

    def test_an_image_comes_back_as_mcp_image_content(self, server, tmp_path):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
        (tmp_path / "shot.png").write_bytes(buffer.getvalue())
        mcp = server()
        mcp.initialize()
        reply = mcp.call(1, "Read", {"file_path": str(tmp_path / "shot.png")})
        images = [b for b in reply["result"]["content"] if b["type"] == "image"]
        assert len(images) == 1
        assert images[0]["mimeType"] == "image/png"
        assert images[0]["data"]

    def test_the_hellos_text_cap_cuts_a_result(self, server, tmp_path):
        (tmp_path / "big.txt").write_text("x" * 5000 + "\n")
        mcp = server(text_cap=200)
        mcp.initialize()
        text = _text(mcp.call(1, "Read", {"file_path": str(tmp_path / "big.txt")}))
        assert "[Output cut at 200 of" in text
        assert len(text) < 1000

    def test_notifications_cancelled_aborts_a_slow_bash(self, server):
        """The abort reaches the tool, so its exclusive lock is released and
        the next Bash runs, rather than waiting out the sleep."""
        mcp = server()
        mcp.initialize()
        mcp.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "Bash", "arguments": {"command": "sleep 30"}}})
        time.sleep(0.3)
        mcp.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
        started = time.monotonic()
        after = mcp.call(6, "Bash", {"command": "echo after"})
        assert after["id"] == 6
        assert "after" in _text(after)
        assert time.monotonic() - started < 10

    @pytest.mark.parametrize("over_socket", [False, True])
    def test_a_bash_childs_stdin_is_not_the_mcp_stream(self, server, over_socket):
        """Bash inherits the server's stdin. Under `--fd` that is /dev/null;
        here it would be the CLI's stream, so a child could swallow requests
        or, on the CLI's socket, write replies of its own."""
        probe = (
            "if [ -p /dev/stdin ] || [ -S /dev/stdin ]; then echo stdin-is-a-stream; "
            "else echo stdin-is-not-a-stream; fi"
        )
        mcp = server(over_socket=over_socket)
        mcp.initialize()
        reply = mcp.call(1, "Bash", {"command": probe})
        assert "stdin-is-not-a-stream" in _text(reply)
        assert mcp.request(2, "ping") == {"jsonrpc": "2.0", "id": 2, "result": {}}

    def test_it_serves_over_a_socketpair(self, server, tmp_path):
        (tmp_path / "note.txt").write_text("over a socket\n")
        mcp = server(over_socket=True)
        mcp.initialize()
        reply = mcp.call(1, "Read", {"file_path": str(tmp_path / "note.txt")})
        assert "over a socket" in _text(reply)
        assert mcp.close_stdin_and_wait() == 0


class TestTheGate:
    def test_the_first_call_waits_for_the_byte_and_the_second_does_not(self, server, tmp_path):
        gate = tmp_path / "gate.fifo"
        os.mkfifo(gate, 0o600)
        (tmp_path / "note.txt").write_text("gated\n")
        mcp = server(gate=str(gate))
        mcp.initialize()
        # Not gated: the CLI needs these to build `system/init`.
        assert mcp.request(1, "tools/list")["result"]["tools"]
        assert mcp.request(2, "ping")["result"] == {}

        mcp.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "Read", "arguments": {"file_path": str(tmp_path / "note.txt")}}})
        assert mcp.no_reply_within(_QUIET), "a call ran before the gate opened"
        assert mcp.request(4, "ping")["id"] == 4

        fd = _open_gate_writer(gate)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
        released = mcp.reply()
        assert released["id"] == 3
        assert "gated" in _text(released)

        again = mcp.call(5, "Read", {"file_path": str(tmp_path / "note.txt")})
        assert "gated" in _text(again)

    def test_a_gate_that_never_opens_runs_nothing(self, server, tmp_path):
        gate = tmp_path / "gate.fifo"
        os.mkfifo(gate, 0o600)
        mcp = server(gate=str(gate))
        mcp.initialize()
        mcp.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "Write", "arguments": {"file_path": str(tmp_path / "w"), "content": "x"}}})
        assert mcp.no_reply_within(_QUIET)
        assert mcp.close_stdin_and_wait() == 0
        assert not (tmp_path / "w").exists()

    def test_a_gate_that_is_not_a_fifo_fails_the_start(self, server, tmp_path):
        plain = tmp_path / "gate.fifo"
        plain.write_text("1")
        mcp = server(gate=str(plain))
        assert mcp.proc.wait(timeout=_TIMEOUT) == 2
        assert mcp.raw_lines == [] or mcp.no_reply_within(0.1)


class TestLifecycle:
    def test_stdin_eof_ends_the_process(self, server):
        mcp = server()
        mcp.initialize()
        assert mcp.close_stdin_and_wait() == 0

    def test_nothing_on_stdout_but_json_rpc(self, server, tmp_path):
        mcp = server()
        mcp.initialize()
        mcp.request(1, "tools/list")
        mcp.call(2, "Bash", {"command": "echo to-stdout; echo to-stderr >&2"})
        mcp.call(3, "Read", {"file_path": str(tmp_path / "missing")})
        mcp.call(4, "Nope", {})
        assert mcp.close_stdin_and_wait() == 0
        time.sleep(0.1)
        assert len(mcp.raw_lines) == 5
        for line in mcp.raw_lines:
            message = json.loads(line)
            assert message["jsonrpc"] == "2.0"


class TestAHelloThatCannotBeRead:
    def _run(self, argv):
        return subprocess.run(argv, capture_output=True, timeout=_TIMEOUT, stdin=subprocess.DEVNULL)

    def test_a_missing_hello_exits_2_with_nothing_on_stdout(self, tmp_path):
        proc = self._run(_argv(tmp_path / "missing.json"))
        assert proc.returncode == 2
        assert proc.stdout == b""
        assert b"hello" in proc.stderr

    def test_a_symlinked_hello_is_refused(self, tmp_path):
        real = _write_hello(tmp_path, _hello(tmp_path))
        link = tmp_path / "link.json"
        link.symlink_to(real)
        proc = self._run(_argv(link))
        assert proc.returncode == 2
        assert proc.stdout == b""

    def test_a_hello_that_is_not_an_object_is_refused(self, tmp_path):
        path = tmp_path / "hello.json"
        path.write_text("[1, 2]")
        proc = self._run(_argv(path))
        assert proc.returncode == 2
        assert proc.stdout == b""

    def test_an_unsupported_protocol_is_refused(self, tmp_path):
        hello = _hello(tmp_path)
        hello["protocol"] = 999
        proc = self._run(_argv(_write_hello(tmp_path, hello)))
        assert proc.returncode == 2
        assert proc.stdout == b""

    def test_a_bad_tools_field_is_refused(self, tmp_path):
        hello = _hello(tmp_path)
        hello["tools"] = "Read"
        proc = self._run(_argv(_write_hello(tmp_path, hello)))
        assert proc.returncode == 2
        assert proc.stdout == b""

    def test_mcp_needs_hello_and_excludes_fd(self, tmp_path):
        hello = _write_hello(tmp_path, _hello(tmp_path))
        base = [sys.executable, "-I", "-m", "istota.sandbox.tool_server"]
        assert self._run(base + ["--mcp"]).returncode == 2
        assert self._run(base + ["--mcp", "--hello", str(hello), "--fd", "3"]).returncode == 2
        assert self._run(base).returncode == 2
