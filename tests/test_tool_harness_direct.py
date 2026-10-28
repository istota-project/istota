"""The tool harness on direct spawn (unified tool server, #862, Stage 5).

The CLI starts the tool server itself, from the harness's `mcp_config.json`,
through `sandbox/tool_launcher.py`; the daemon only writes the files, reads
the pid the launcher reports, checks it descends from the CLI, authorizes it
and watches it. No bwrap here: `tool_server_wrap` is the identity or `None`,
so the launcher execs the tool server directly, as on a developer machine.

The CLI is played by `_RELAY_CLI`, a small script that spawns the launcher
from `mcp_config.json` with stdio pipes, as the pinned CLI does, and relays
MCP lines between its own stdio and the server's. The test speaks MCP to it;
it is the CLI root `set_cli_root` names, and the launcher is its child.
"""

import json
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from istota.agent.events import WEB_FETCH_MCP_SERVER
from istota.brain import _tool_harness as harness_mod
from istota.brain import _web_fetch_mcp as web_mcp
from istota.brain._tool_harness import (
    HARNESS_MCP_SERVER,
    release_calls,
    set_cli_root,
    tool_harness,
)
from istota.brain._types import BrainRequest, HarnessSpec
from istota.sandbox.peer_process import parent_pid, start_time
from istota.session.tools import WebFetchPolicy
from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS

_TIMEOUT = 30.0
_SIX = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]

_RELAY_CLI = r"""
import json, subprocess, sys, threading
server = json.load(open(sys.argv[1]))["mcpServers"]["istota"]
proc = subprocess.Popen([server["command"], *server["args"]],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def up():
    for line in proc.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    proc.wait()
threading.Thread(target=up, daemon=True).start()
for line in sys.stdin.buffer:
    if line == b"RESPAWN\n":
        # What the pinned CLI does with a server that died: start it again.
        subprocess.Popen([server["command"], *server["args"]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        continue
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
    except OSError:
        pass
try:
    proc.stdin.close()
except OSError:
    pass
proc.wait()
"""


def _req(tmp_path, *, tools=None, harness=True, web_fetch=None, control=True, **kw):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    root = tmp_path / "harness-root"
    root.mkdir(exist_ok=True)
    control_dir = None
    if control:
        control_dir = tmp_path / "control"
        control_dir.mkdir(mode=0o700, exist_ok=True)
    spec = HarnessSpec(
        cli_env={}, harness_root=root, web_fetch=web_fetch, control_dir=control_dir,
    ) if harness else None
    base = dict(
        prompt="",
        allowed_tools=list(_SIX if tools is None else tools),
        cwd=workspace,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_CODE_OAUTH_TOKEN": "not-for-the-tool-server",
        },
        timeout_seconds=60,
        task_id=7,
        attempt=1,
        harness=spec,
    )
    base.update(kw)
    return BrainRequest(**base)


class _Recorder:
    def __init__(self):
        self.pids: list[int] = []
        self.failed = threading.Event()

    def on_pid(self, pid: int) -> None:
        self.pids.append(pid)

    def wait_for_pid(self, timeout: float = _TIMEOUT) -> int:
        deadline = time.monotonic() + timeout
        while not self.pids and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.pids, "no pid was reported"
        return self.pids[0]


@pytest.fixture
def rec():
    return _Recorder()


def _harness(req, rec):
    req.on_pid = rec.on_pid
    return tool_harness(req, brain_label="test brain", on_failure=rec.failed.set)


def _server_entry(handle) -> dict:
    return json.loads(handle.mcp_config_path.read_text())["mcpServers"][HARNESS_MCP_SERVER]


def _arg_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class _Cli:
    """The relay CLI: spawns the launcher from the harness's MCP config."""

    def __init__(self, handle):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _RELAY_CLI, str(handle.mcp_config_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            cwd=str(handle.cwd),
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "the-cli-credential"},
        )
        self.pid = self.proc.pid
        self._lines: queue.Queue = queue.Queue()
        self._next = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def send(self, method, params=None, *, request_id=None) -> int:
        if request_id is None:
            self._next += 1
            request_id = self._next
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()
        return request_id

    def reply(self, timeout: float = _TIMEOUT) -> dict | None:
        line = self._lines.get(timeout=timeout)
        return None if line is None else json.loads(line)

    def request(self, method, params=None) -> dict:
        self.send(method, params)
        return self.reply()

    def handshake(self) -> list[dict]:
        init = self.request("initialize", {"protocolVersion": "2025-11-25"})
        assert "result" in init, init
        return self.request("tools/list")["result"]["tools"]

    def end(self) -> int:
        """What the CLI does at the end of a session: closes the server's stdio."""
        self.proc.stdin.close()
        return self.proc.wait(timeout=_TIMEOUT)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=_TIMEOUT)


@pytest.fixture
def clis():
    started: list[_Cli] = []
    yield started
    for cli in started:
        cli.kill()


def _start_cli(handle, clis) -> _Cli:
    cli = _Cli(handle)
    clis.append(cli)
    set_cli_root(handle, cli.pid)
    return cli


@pytest.fixture
def stranger():
    """A live process that is no descendant of any CLI here."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _report(handle, pid: int) -> None:
    fifo = _arg_after(_server_entry(handle)["args"], "--report")
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, b"%d\n" % pid)
    finally:
        os.close(fd)


def _wait(predicate, timeout: float = _TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---- The files --------------------------------------------------------------


def test_the_harness_writes_the_launcher_config_the_hello_and_the_fifos(tmp_path, rec):
    req = _req(tmp_path, tools=["Read", "Bash", "WebSearch"])
    with _harness(req, rec) as handle:
        assert handle.failure is None
        harness_dir = handle.mcp_config_path.parent
        assert harness_dir.parent == tmp_path / "harness-root"
        assert harness_dir.name.startswith("h-1-")
        # WebSearch is the CLI's own; only the six reach the tool server.
        assert handle.offered == ("Read", "Bash")
        assert handle.cwd == tmp_path / "ws"

        server = _server_entry(handle)
        assert server["type"] == "stdio"
        assert server["command"] == sys.executable
        assert server["env"] == {}
        args = server["args"]
        assert args[:3] == ["-I", "-m", "istota.sandbox.tool_launcher"]
        fifo = Path(_arg_after(args, "--report"))
        env_file = Path(_arg_after(args, "--env-file"))
        command = args[args.index("--") + 1:]
        assert fifo.parent == harness_dir and env_file.parent == harness_dir
        assert os.path.isabs(command[0])
        assert command[1:4] == ["-m", "istota.sandbox.tool_server", "--mcp"]
        hello_path = Path(_arg_after(command, "--hello"))
        assert hello_path.parent == tmp_path / "control"

        assert stat.S_ISFIFO(fifo.stat().st_mode)
        for path in (handle.mcp_config_path, env_file, hello_path, fifo):
            assert stat.S_IMODE(path.lstat().st_mode) == 0o600, path

        env = json.loads(env_file.read_text())
        assert env["PATH"] == req.env["PATH"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

        hello = json.loads(hello_path.read_text())
        assert hello["protocol"] >= 1
        assert hello["tools"] == ["Read", "Bash"]
        assert hello["text_cap"] == HARNESS_TEXT_CAP_CHARS
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in hello["subprocess_env"]
        gate = Path(hello["gate"])
        assert gate.parent == tmp_path / "control"
        assert stat.S_ISFIFO(gate.lstat().st_mode)
        assert stat.S_IMODE(gate.lstat().st_mode) == 0o600

        # No socket anywhere: nothing in the daemon listens for tool calls.
        assert "--socket" not in args
        assert not [p for p in harness_dir.rglob("*") if stat.S_ISSOCK(p.lstat().st_mode)]
    assert rec.pids == []
    assert not rec.failed.is_set()


def test_without_webfetch_there_is_one_server(tmp_path, rec):
    req = _req(tmp_path, tools=["Read", "Bash"])
    with _harness(req, rec) as handle:
        assert handle.failure is None
        servers = json.loads(handle.mcp_config_path.read_text())["mcpServers"]
        assert list(servers) == [HARNESS_MCP_SERVER]
        assert handle.mcp_servers == (HARNESS_MCP_SERVER,)
        assert handle.mcp_tools == ("mcp__istota__Read", "mcp__istota__Bash")


@pytest.mark.parametrize("tools,policy", [
    (["Read", "WebFetch"], None),  # the grant has it, the policy is off
    (["Read"], WebFetchPolicy()),  # a policy, but the grant left it out
    (["Read", "WebFetch"], WebFetchPolicy(enabled=False)),
], ids=["no-policy", "not-granted", "disabled"])
def test_webfetch_needs_both_the_grant_and_the_policy(tmp_path, rec, tools, policy):
    req = _req(tmp_path, tools=tools, web_fetch=policy)
    with _harness(req, rec) as handle:
        assert handle.failure is None
        assert handle.mcp_servers == (HARNESS_MCP_SERVER,)
        assert not any(name.startswith("mcp__istota_web__") for name in handle.mcp_tools)
        assert not list(handle.mcp_config_path.parent.glob("web-*"))


def _web_entry(handle) -> dict:
    return json.loads(handle.mcp_config_path.read_text())["mcpServers"][WEB_FETCH_MCP_SERVER]


def test_webfetch_is_a_second_server_outside_the_sandbox(tmp_path, rec):
    """The tool server offers the six; WebFetch is `istota_web`, spawned by the
    CLI unwrapped, with its policy and URL corpus in a 0600 file."""
    wrapped = []

    def wrap(cmd):
        wrapped.append(cmd)
        return cmd

    policy = WebFetchPolicy(require_url_provenance=True, block_hosts=("blocked.example",))
    req = _req(
        tmp_path, tools=["Read", "WebFetch"], web_fetch=policy,
        prompt="please read https://example.org/a and nothing else",
        tool_server_wrap=wrap,
    )
    with _harness(req, rec) as handle:
        assert handle.failure is None
        assert handle.offered == ("Read",)
        assert handle.mcp_servers == (HARNESS_MCP_SERVER, WEB_FETCH_MCP_SERVER)
        assert handle.mcp_tools == ("mcp__istota__Read", "mcp__istota_web__WebFetch")

        harness_dir = handle.mcp_config_path.parent
        entry = _web_entry(handle)
        assert entry["type"] == "stdio"
        assert entry["command"] == sys.executable
        assert entry["env"] == {}
        assert entry["args"][:4] == ["-I", "-m", "istota.brain._web_fetch_mcp", "--config"]
        assert len(entry["args"]) == 5
        config = Path(entry["args"][4])
        assert config.parent == harness_dir
        assert stat.S_IMODE(config.lstat().st_mode) == 0o600
        # Only the tool server is wrapped.
        assert len(wrapped) == 1 and "istota.sandbox.tool_server" in " ".join(wrapped[0])

        document = web_mcp.read_config(str(config))
        assert web_mcp.policy_from_document(document) == policy
        assert web_mcp.corpus_from_document(document) == frozenset({"https://example.org/a"})
        gate = Path(document["gate"])
        assert gate.parent == harness_dir
        assert stat.S_ISFIFO(gate.lstat().st_mode)
        assert stat.S_IMODE(gate.lstat().st_mode) == 0o600
        assert "not-for-the-tool-server" not in config.read_text()
        files = [config, gate]
    for path in files:
        assert not os.path.lexists(path), path


def _gate_has_reader(path: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


def test_the_web_server_waits_for_release_and_reports_no_pid(tmp_path, rec, clis):
    policy = WebFetchPolicy(block_hosts=("blocked.example",))
    req = _req(tmp_path, tools=["Read", "WebFetch"], web_fetch=policy)
    with _harness(req, rec) as handle:
        # The tool server reports, as it must before calls are released; the
        # web server is spawned by hand here and never does.
        _start_cli(handle, clis).handshake()
        server_pid = rec.wait_for_pid()
        entry = _web_entry(handle)
        proc = subprocess.Popen(
            [entry["command"], *entry["args"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "the-cli-credential"},
        )
        lines: queue.Queue = queue.Queue()

        def pump():
            for line in proc.stdout:
                lines.put(json.loads(line))

        threading.Thread(target=pump, daemon=True).start()

        def send(request_id, method, params=None):
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            proc.stdin.flush()

        try:
            send(0, "initialize", {"protocolVersion": "2025-11-25"})
            assert "result" in lines.get(timeout=_TIMEOUT)
            send(1, "tools/call", {"name": "WebFetch", "arguments": {"url": "https://blocked.example/"}})
            with pytest.raises(queue.Empty):
                lines.get(timeout=0.6)
            gate = json.loads(Path(entry["args"][4]).read_text())["gate"]
            assert _wait(lambda: _gate_has_reader(gate))
            release_calls(handle)
            reply = lines.get(timeout=_TIMEOUT)
            text = "".join(block["text"] for block in reply["result"]["content"])
            assert "host blocked.example is blocked" in text
        finally:
            proc.kill()
            proc.wait(timeout=_TIMEOUT)
        assert rec.pids == [server_pid]
    assert not rec.failed.is_set()


# ---- The happy path ---------------------------------------------------------


def test_a_call_runs_through_the_launched_server_once_calls_are_released(tmp_path, rec, clis):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "note.txt").write_text("held then released\n")
    req = _req(tmp_path, tools=["Read", "Grep", "Glob"])
    with _harness(req, rec) as handle:
        cli = _start_cli(handle, clis)
        listed = cli.handshake()
        assert [t["name"] for t in listed] == ["Read", "Grep", "Glob"]

        server_pid = rec.wait_for_pid()
        # The tool server, a child of the CLI, is what was authorized; never the CLI.
        assert server_pid != cli.pid
        assert parent_pid(server_pid) == cli.pid
        assert cli.pid not in rec.pids

        call = cli.send("tools/call", {
            "name": "Read", "arguments": {"file_path": str(tmp_path / "ws" / "note.txt")},
        })
        with pytest.raises(queue.Empty):
            cli.reply(timeout=0.5)
        release_calls(handle)
        reply = cli.reply()
        assert reply["id"] == call
        assert reply["result"]["isError"] is False
        assert "held then released" in reply["result"]["content"][0]["text"]

        assert cli.end() == 0
        assert _wait(lambda: rec.pids[-1:] == [0])
    assert rec.pids == [server_pid, 0]
    assert not rec.failed.is_set()
    assert handle.failure is None


def test_the_tool_server_runs_in_the_requests_cwd(tmp_path, rec, clis):
    scratch = tmp_path / "scratch"
    workspace = tmp_path / "workspace"
    for path in (scratch, workspace / "sub"):
        path.mkdir(parents=True, exist_ok=True)
    req = _req(
        tmp_path, cwd=workspace / "sub",
        fs_read_roots=[scratch, workspace], fs_write_roots=[scratch, workspace],
    )
    with _harness(req, rec) as handle:
        assert handle.cwd == workspace / "sub"
        cli = _start_cli(handle, clis)
        cli.handshake()
        release_calls(handle)
        reply = cli.request("tools/call", {"name": "Bash", "arguments": {"command": "pwd -P"}})
        assert reply["result"]["isError"] is False
        assert Path(reply["result"]["content"][0]["text"].strip()) == (workspace / "sub").resolve()
        cli.end()


# ---- Pid reports the daemon refuses -------------------------------------------


def test_a_report_from_outside_the_cli_is_refused_killed_and_fails_the_attempt(
    tmp_path, rec, stranger, caplog,
):
    req = _req(tmp_path)
    cli = subprocess.Popen(["sleep", "60"])
    try:
        with _harness(req, rec) as handle:
            set_cli_root(handle, cli.pid)
            _report(handle, stranger.pid)

            assert rec.failed.wait(_TIMEOUT)
            assert "outside the CLI" in handle.failure
            assert stranger.wait(timeout=_TIMEOUT) == -signal.SIGKILL
            assert any(
                str(stranger.pid) in r.getMessage() and r.levelname == "WARNING"
                for r in caplog.records
            )
    finally:
        cli.kill()
        cli.wait()
    assert rec.pids == []


def test_releasing_calls_with_no_server_reported_fails_the_attempt(tmp_path, rec, monkeypatch):
    """A server that never reported was never authorized with the proxies and
    is not `worker_pid`; the gate stays shut and the attempt fails."""
    monkeypatch.setattr(harness_mod, "_REPORT_WAIT_SECONDS", 0.3)
    req = _req(tmp_path)
    cli = subprocess.Popen(["sleep", "60"])
    try:
        with _harness(req, rec) as handle:
            set_cli_root(handle, cli.pid)
            gate = json.loads(Path(_arg_after(
                _server_entry(handle)["args"][_server_entry(handle)["args"].index("--") + 1:],
                "--hello",
            )).read_text())["gate"]
            reader = os.open(gate, os.O_RDONLY | os.O_NONBLOCK)
            try:
                release_calls(handle)
                assert rec.failed.is_set()
                assert "never reported" in handle.failure
                # No writer has ever opened it: end-of-file, no byte.
                assert os.read(reader, 1) == b""
            finally:
                os.close(reader)
    finally:
        cli.kill()
        cli.wait()
    assert rec.pids == []


def test_a_report_with_no_cli_registered_is_refused(tmp_path, rec, stranger, monkeypatch):
    monkeypatch.setattr(harness_mod, "_ROOT_GRACE_SECONDS", 0.2)
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        _report(handle, stranger.pid)
        assert rec.failed.wait(_TIMEOUT)
        assert "before the CLI was registered" in handle.failure
        assert stranger.wait(timeout=_TIMEOUT) == -signal.SIGKILL
    assert rec.pids == []


def test_a_second_report_under_one_cli_is_refused(tmp_path, rec, clis):
    """One tool server per CLI spawn: a launcher the same CLI starts again
    reports again, and that report is never authorized."""
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        cli = _start_cli(handle, clis)
        cli.handshake()
        first = rec.wait_for_pid()

        cli.proc.stdin.write(b"RESPAWN\n")
        cli.proc.stdin.flush()

        assert rec.failed.wait(_TIMEOUT)
        assert "second tool server" in handle.failure
        assert [p for p in rec.pids if p] == [first]
        cli.kill()


# ---- The watcher --------------------------------------------------------------


def test_a_server_killed_between_calls_fails_the_attempt(tmp_path, rec, clis):
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        cli = _start_cli(handle, clis)
        cli.handshake()
        server_pid = rec.wait_for_pid()
        release_calls(handle)

        os.kill(server_pid, signal.SIGKILL)

        # The CLI is still running, with nothing in flight: the watcher fails
        # the attempt within its grace and calls on_failure.
        assert rec.failed.wait(_TIMEOUT)
        assert handle.failure == "the tool server exited"
        assert cli.proc.poll() is None
        assert rec.pids == [server_pid, 0]
        cli.kill()
    assert rec.pids == [server_pid, 0]


def test_a_normal_end_is_not_a_failure(tmp_path, rec, clis):
    """The server goes first, the CLI a moment later: no failure."""
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        cli = _start_cli(handle, clis)
        cli.handshake()
        rec.wait_for_pid()
        assert cli.end() == 0
        time.sleep(harness_mod._EXIT_GRACE_SECONDS + 0.5)
        assert handle.failure is None
    assert not rec.failed.is_set()


# ---- Close and respawn --------------------------------------------------------


def test_close_kills_a_live_server_removes_the_files_and_reports_the_reap_once(
    tmp_path, rec, clis, monkeypatch,
):
    monkeypatch.setattr(harness_mod, "_CLOSE_WAIT_SECONDS", 0.5)
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        server = _server_entry(handle)
        args = server["args"]
        command = args[args.index("--") + 1:]
        hello_path = Path(_arg_after(command, "--hello"))
        gate = Path(json.loads(hello_path.read_text())["gate"])
        files = [
            handle.mcp_config_path, Path(_arg_after(args, "--report")),
            Path(_arg_after(args, "--env-file")), hello_path, gate,
        ]
        cli = _start_cli(handle, clis)
        cli.handshake()
        server_pid = rec.wait_for_pid()
        server_start = start_time(server_pid)
    # The CLI still holds the server's stdin open; close kills it.
    assert _wait(lambda: start_time(server_pid) != server_start)
    for path in files:
        assert not os.path.lexists(path), path
    assert rec.pids == [server_pid, 0]
    assert not rec.failed.is_set()


def test_a_respawned_cli_gets_its_own_server_and_the_old_one_goes(tmp_path, rec, clis):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "note.txt").write_text("second cli\n")
    req = _req(tmp_path)
    with _harness(req, rec) as handle:
        first_cli = _start_cli(handle, clis)
        first_cli.handshake()
        first = rec.wait_for_pid()
        first_start = start_time(first)

        second_cli = _start_cli(handle, clis)
        assert _wait(lambda: start_time(first) != first_start)
        second_cli.handshake()
        assert _wait(lambda: len([p for p in rec.pids if p]) == 2)
        second = [p for p in rec.pids if p][1]
        assert second != first and parent_pid(second) == second_cli.pid

        release_calls(handle)
        reply = second_cli.request("tools/call", {
            "name": "Read", "arguments": {"file_path": str(tmp_path / "ws" / "note.txt")},
        })
        assert "second cli" in reply["result"]["content"][0]["text"]
        assert handle.failure is None
        second_cli.end()
        first_cli.kill()
    assert not rec.failed.is_set()
    assert first_cli.pid not in rec.pids and second_cli.pid not in rec.pids


# ---- Start failures -----------------------------------------------------------


def test_a_start_failure_is_reported_on_the_handle(tmp_path, rec):
    with _harness(_req(tmp_path, harness=False), rec) as handle:
        assert handle.failure and "no harness spec" in handle.failure

    with _harness(_req(tmp_path, tools=[]), rec) as handle:
        assert handle.failure and "grants no tools" in handle.failure

    req = _req(tmp_path)
    req.harness = HarnessSpec(
        cli_env={}, harness_root=tmp_path / "missing", control_dir=tmp_path / "control",
    )
    with _harness(req, rec) as handle:
        assert handle.failure and "harness directory" in handle.failure
    assert not rec.failed.is_set()
    assert rec.pids == []


def test_a_sandboxed_request_with_no_control_dir_is_refused(tmp_path, rec):
    """The sandboxed tool server reads its hello and its gate from a directory
    the wrap binds; with no control directory there is none to use."""
    def a_real_wrap(cmd):
        return ["/usr/bin/env", *cmd]

    req = _req(tmp_path, control=False, tool_server_wrap=a_real_wrap)
    with _harness(req, rec) as handle:
        assert handle.failure and "control directory" in handle.failure


def test_unwrapped_with_no_control_dir_uses_the_harness_dir(tmp_path, rec, clis):
    req = _req(tmp_path, control=False, tools=["Read"])
    with _harness(req, rec) as handle:
        assert handle.failure is None
        command = _server_entry(handle)["args"]
        hello_path = Path(_arg_after(command, "--hello"))
        assert hello_path.parent == handle.mcp_config_path.parent
        cli = _start_cli(handle, clis)
        assert [t["name"] for t in cli.handshake()] == ["Read"]
        cli.end()
