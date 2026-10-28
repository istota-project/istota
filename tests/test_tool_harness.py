"""The tool harness's bridge, with a real tool server, unwrapped.

No bwrap here: `tool_server_wrap` is `None`, so the tool server runs as a
plain subprocess, as it does on a developer machine. What is held is the
relay: calls held until `release_calls`, the peer check and `set_cli_root`'s
revocation, a dead tool server failing calls and firing `on_failure`, the
socket's lifetime and the reap report. One integration test goes through the
real seam: the adapter subprocess, the bridge, the dispatcher and the tool
server, reading a file.

The tests are synchronous on purpose: the harness runs its own loop on its own
thread, and the test speaks to it over blocking sockets.

The cwd rule (`request_tool_workspace`) is tested here for both brains.
"""

import json
import os
import queue
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from istota.brain import _tool_harness as harness_mod
from istota.brain._tool_harness import (
    HARNESS_MCP_SERVER,
    release_calls,
    request_tool_workspace,
    set_cli_root,
    tool_harness,
)
from istota.brain._types import BrainRequest, HarnessSpec
from istota.sandbox import tool_server_protocol as proto
from istota.session.tools import WebFetchPolicy

_TIMEOUT = 30.0
_SIX = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


def _req(tmp_path, *, tools=None, harness=True, harness_spec=None, **kw):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    root = tmp_path / "harness-root"
    root.mkdir(exist_ok=True)
    base = dict(
        prompt="",
        allowed_tools=list(_SIX if tools is None else tools),
        cwd=workspace,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout_seconds=60,
        task_id=7,
        attempt=1,
        harness=harness_spec or (HarnessSpec(cli_env={}, harness_root=root) if harness else None),
    )
    base.update(kw)
    return BrainRequest(**base)


def _socket_of(handle) -> str:
    config = json.loads(handle.mcp_config_path.read_text())
    args = config["mcpServers"][HARNESS_MCP_SERVER]["args"]
    return args[args.index("--socket") + 1]


class _Client:
    """A blocking framing client, standing where the adapter would."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(_TIMEOUT)
        self.sock.connect(path)
        self._decoder = proto.FrameDecoder()
        self._frames: list[dict] = []

    def send(self, message: dict) -> None:
        self.sock.sendall(proto.encode(message))

    def next_frame(self, timeout: float = _TIMEOUT) -> dict | None:
        """The next frame, or `None` when the bridge closed the connection."""
        self.sock.settimeout(timeout)
        while not self._frames:
            try:
                data = self.sock.recv(65536)
            except ConnectionResetError:
                return None
            if not data:
                return None
            self._frames.extend(self._decoder.feed(data))
        return self._frames.pop(0)

    def hello(self) -> dict | None:
        self.send({"type": proto.MSG_HELLO, "protocol": proto.PROTOCOL_VERSION})
        return self.next_frame()

    def close(self):
        self.sock.close()


@pytest.fixture
def pids():
    return []


@pytest.fixture
def failures():
    return threading.Event()


def _harness(req, failures):
    return tool_harness(req, brain_label="test brain", on_failure=failures.set)


def test_the_harness_lays_out_its_directory_socket_and_config(tmp_path, pids, failures):
    req = _req(tmp_path, tools=["Read", "Bash", "WebSearch"], on_pid=pids.append)
    with _harness(req, failures) as handle:
        assert handle.failure is None
        harness_dir = handle.mcp_config_path.parent
        assert harness_dir.parent == tmp_path / "harness-root"
        assert harness_dir.name.startswith("h-1-")
        for path in (harness_dir, handle.config_dir, handle.home_dir, handle.tmp_dir):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert path.is_dir()
        assert sorted(p.name for p in handle.config_dir.iterdir()) == [
            ".claude.json", "settings.json",
        ]

        config = json.loads(handle.mcp_config_path.read_text())
        server = config["mcpServers"][HARNESS_MCP_SERVER]
        assert server["command"] == sys.executable
        assert server["args"][:3] == ["-I", "-m", "istota.sandbox.mcp_adapter"]
        assert server["env"] == {}
        assert stat.S_IMODE(handle.mcp_config_path.stat().st_mode) == 0o600

        path = _socket_of(handle)
        assert os.path.dirname(path) == tempfile.gettempdir()
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        # WebSearch is not one of the six; it never reaches the tools list.
        assert handle.offered == ("Read", "Bash")
        assert handle.cwd == tmp_path / "ws"
        server_pid = pids[0]

    assert not os.path.exists(path)
    assert pids == [server_pid, 0]
    assert not failures.is_set()


def test_a_call_waits_until_release_calls(tmp_path, failures):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "note.txt").write_text("held then released\n")
    req = _req(tmp_path)
    with _harness(req, failures) as handle:
        set_cli_root(handle, os.getpid())
        client = _Client(_socket_of(handle))
        try:
            ready = client.hello()
            assert ready["type"] == proto.MSG_READY
            assert [t["name"] for t in ready["tools"]] == _SIX
            bash = next(t for t in ready["tools"] if t["name"] == "Bash")
            assert "exclude_from_context" not in bash["input_schema"]["properties"]

            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "Read",
                         "args": {"file_path": str(tmp_path / "ws" / "note.txt")}})
            with pytest.raises(socket.timeout):
                client.next_frame(timeout=0.5)

            release_calls(handle)
            result = client.next_frame()
            assert result["type"] == proto.MSG_RESULT and result["id"] == "1"
            assert result["is_error"] is False
            assert "held then released" in result["content"][0]["text"]
        finally:
            client.close()


def test_a_respawned_cli_has_its_calls_held_again(tmp_path, failures):
    """The transient retry respawns the CLI under one harness; the new CLI's
    `system/init` has to pass before its calls run."""
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "note.txt").write_text("second cli\n")
    req = _req(tmp_path)
    with _harness(req, failures) as handle:
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        set_cli_root(handle, os.getpid())
        client = _Client(_socket_of(handle))
        try:
            client.hello()
            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "Read",
                         "args": {"file_path": str(tmp_path / "ws" / "note.txt")}})
            with pytest.raises(socket.timeout):
                client.next_frame(timeout=0.5)
            release_calls(handle)
            assert "second cli" in client.next_frame()["content"][0]["text"]
        finally:
            client.close()


def test_a_name_the_grant_left_out_is_refused_and_not_run(tmp_path, failures):
    req = _req(tmp_path, tools=["Read"])
    with _harness(req, failures) as handle:
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        client = _Client(_socket_of(handle))
        try:
            client.hello()
            marker = tmp_path / "ws" / "ran"
            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "Bash",
                         "args": {"command": f"touch {marker}"}})
            result = client.next_frame()
            assert result["is_error"] is True
            assert "Unknown tool" in result["content"][0]["text"]
            assert not marker.exists()
        finally:
            client.close()


def _web_fetch_req(tmp_path, *, tools, policy, prompt=""):
    root = tmp_path / "harness-root"
    root.mkdir(exist_ok=True)
    spec = HarnessSpec(cli_env={}, harness_root=root, web_fetch=policy)
    return _req(tmp_path, tools=tools, harness=False, harness_spec=spec, prompt=prompt)


def test_webfetch_is_offered_and_runs_in_the_daemon_under_the_policy(tmp_path, failures):
    """The spec's policy is the one the tool applies: provenance on and a URL
    the prompt never named is refused before anything is resolved."""
    policy = WebFetchPolicy(require_url_provenance=True)
    req = _web_fetch_req(
        tmp_path, tools=["Read", "WebFetch"], policy=policy,
        prompt="Read https://named.example.test/page",
    )
    with _harness(req, failures) as handle:
        assert handle.failure is None
        assert handle.offered == ("Read", "WebFetch")
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        client = _Client(_socket_of(handle))
        try:
            ready = client.hello()
            fetch = next(t for t in ready["tools"] if t["name"] == "WebFetch")
            assert fetch["read_only"] is True
            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "WebFetch",
                         "args": {"url": "https://other.example.test/", "prompt": "x"}})
            result = client.next_frame()
            assert result["is_error"] is True
            assert "require_url_provenance" in result["content"][0]["text"]
        finally:
            client.close()


def test_webfetch_is_not_offered_without_a_policy(tmp_path, failures):
    req = _web_fetch_req(tmp_path, tools=["Read", "WebFetch"], policy=None)
    with _harness(req, failures) as handle:
        assert handle.failure is None
        assert handle.offered == ("Read",)


def test_webfetch_is_not_offered_when_the_grant_leaves_it_out(tmp_path, failures):
    req = _web_fetch_req(tmp_path, tools=["Read"], policy=WebFetchPolicy())
    with _harness(req, failures) as handle:
        assert handle.failure is None
        assert handle.offered == ("Read",)


@pytest.fixture
def stranger():
    """A live process that is no ancestor of this test."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


def test_a_peer_outside_the_cli_is_refused_and_a_new_root_revokes_the_old(
    tmp_path, failures, stranger, monkeypatch, caplog,
):
    monkeypatch.setattr(harness_mod, "_PEER_GRACE_SECONDS", 0.2)
    req = _req(tmp_path)
    with _harness(req, failures) as handle:
        path = _socket_of(handle)

        set_cli_root(handle, stranger)
        refused = _Client(path)
        assert refused.hello() is None
        refused.close()
        assert any("refused a connection" in r.getMessage() for r in caplog.records)

        set_cli_root(handle, os.getpid())
        admitted = _Client(path)
        assert admitted.hello()["type"] == proto.MSG_READY

        set_cli_root(handle, stranger)
        # The previous CLI's connection is dropped, and a new one is refused.
        assert admitted.next_frame() is None
        admitted.close()
        again = _Client(path)
        assert again.hello() is None
        again.close()


def test_a_killed_tool_server_fails_the_pending_call_and_fires_on_failure(
    tmp_path, pids, failures,
):
    req = _req(tmp_path, on_pid=pids.append)
    with _harness(req, failures) as handle:
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        client = _Client(_socket_of(handle))
        try:
            client.hello()
            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "Bash",
                         "args": {"command": "sleep 30"}})
            time.sleep(0.5)
            os.killpg(pids[0], signal.SIGKILL)

            result = client.next_frame()
            assert result["id"] == "1" and result["is_error"] is True
            assert failures.wait(_TIMEOUT)
            assert handle.failure is not None
            assert "SIGKILL" in handle.failure

            client.send({"type": proto.MSG_CALL, "id": "2", "tool": "Read",
                         "args": {"file_path": str(tmp_path / "ws")}})
            later = client.next_frame()
            assert later["id"] == "2" and later["is_error"] is True
        finally:
            client.close()
    assert pids[-1] == 0


def test_a_start_failure_is_reported_on_the_handle(tmp_path, failures):
    with _harness(_req(tmp_path, harness=False), failures) as handle:
        assert handle.failure and "no harness spec" in handle.failure

    with _harness(_req(tmp_path, tools=[]), failures) as handle:
        assert handle.failure and "grants no tools" in handle.failure

    req = _req(tmp_path)
    req.harness = HarnessSpec(cli_env={}, harness_root=tmp_path / "missing")
    with _harness(req, failures) as handle:
        assert handle.failure and "harness directory" in handle.failure
    assert not failures.is_set()


class _McpAdapter:
    """The adapter started from the harness's own `mcp_config.json`."""

    def __init__(self, handle):
        server = json.loads(handle.mcp_config_path.read_text())["mcpServers"][HARNESS_MCP_SERVER]
        self.proc = subprocess.Popen(
            [server["command"], *server["args"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "not-a-token"},
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)

    def request(self, request_id, method, params=None) -> dict:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()
        return json.loads(self._lines.get(timeout=_TIMEOUT))

    def close(self) -> int:
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=_TIMEOUT)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


def test_a_read_goes_from_the_adapter_through_the_bridge_to_the_tool_server(tmp_path, failures):
    (tmp_path / "ws").mkdir()
    target = tmp_path / "ws" / "hello.txt"
    target.write_text("through the real seam\n")
    req = _req(tmp_path, tools=["Read", "Grep", "Glob"])
    with _harness(req, failures) as handle:
        # The adapter is this test's child, as it would be the CLI's.
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        mcp = _McpAdapter(handle)
        try:
            init = mcp.request(1, "initialize", {"protocolVersion": "2025-11-25"})
            assert init["result"]["protocolVersion"] == "2025-11-25"
            listed = mcp.request(2, "tools/list")["result"]["tools"]
            assert [t["name"] for t in listed] == ["Read", "Grep", "Glob"]
            assert all(t["annotations"]["readOnlyHint"] for t in listed)

            reply = mcp.request(3, "tools/call", {"name": "Read", "arguments": {"file_path": str(target)}})
            assert reply["result"]["isError"] is False
            assert "through the real seam" in reply["result"]["content"][0]["text"]
        finally:
            assert mcp.close() == 0
    assert not failures.is_set()


# ---- The cwd rule, on both brains --------------------------------------------


def _roots_req(tmp_path, cwd):
    scratch = tmp_path / "scratch"
    workspace = tmp_path / "workspace"
    for path in (scratch, workspace, workspace / "sub"):
        path.mkdir(parents=True, exist_ok=True)
    return _req(
        tmp_path,
        cwd=cwd,
        fs_read_roots=[scratch, workspace],
        fs_write_roots=[scratch, workspace],
    )


def test_the_cwd_is_the_requests_when_it_lies_in_a_write_root(tmp_path):
    from istota.brain.native import NativeBrain
    from istota.config import NativeBrainConfig

    req = _roots_req(tmp_path, tmp_path / "workspace" / "sub")
    assert request_tool_workspace(req)[0] == tmp_path / "workspace" / "sub"
    native = NativeBrain(NativeBrainConfig(model="m"), provider=object())
    assert native._hello_payload(req)["cwd"] == str(tmp_path / "workspace" / "sub")


def test_the_cwd_falls_back_to_the_first_write_root_outside_them(tmp_path):
    from istota.brain.native import NativeBrain
    from istota.config import NativeBrainConfig

    req = _roots_req(tmp_path, tmp_path)
    assert request_tool_workspace(req)[0] == tmp_path / "scratch"
    # A spelling that only looks inside is outside.
    sneaky = _roots_req(tmp_path, tmp_path / "workspace" / ".." / "elsewhere")
    assert request_tool_workspace(sneaky)[0] == tmp_path / "scratch"
    native = NativeBrain(NativeBrainConfig(model="m"), provider=object())
    assert native._hello_payload(req)["cwd"] == str(tmp_path / "scratch")


def test_the_harness_tool_server_runs_in_the_requests_cwd(tmp_path, failures):
    req = _roots_req(tmp_path, tmp_path / "workspace" / "sub")
    with _harness(req, failures) as handle:
        assert handle.cwd == tmp_path / "workspace" / "sub"
        set_cli_root(handle, os.getpid())
        release_calls(handle)
        client = _Client(_socket_of(handle))
        try:
            client.hello()
            client.send({"type": proto.MSG_CALL, "id": "1", "tool": "Bash", "args": {"command": "pwd -P"}})
            result = client.next_frame()
            assert result["is_error"] is False
            assert Path(result["content"][0]["text"].strip()) == (tmp_path / "workspace" / "sub").resolve()
        finally:
            client.close()
