"""The CLI tool harness on direct spawn, with its tool server under a real bwrap.

`tests/test_tool_harness_direct.py` and `tests/test_claude_code_harness.py`
run the same path with the tool server unwrapped, on any platform. Here the
tool server is wrapped by `build_bwrap_cmd(..., profile=NATIVE)` and started
the way the pinned CLI starts it (#862): a fake `claude`
(`tests/support/fake_claude_cli.py`), holding a sentinel credential in its
environment, spawns `python -I -m istota.sandbox.tool_launcher` from the
harness's `mcp_config.json`; the launcher clears that environment, reports its
pid and execs bwrap, and the tool server reads its hello and call gate from
the task's control dir, bound read-only.

The claim is the spec's: Bash inside the tool server cannot find the CLI's
credential through `env`, `/proc/*/environ`, `ps` or a search of the visible
filesystem; cannot open the harness dir or the pid FIFO; and the skill proxy
refuses a connection from the CLI's process tree while serving the tool
server's. Each probe also runs from the CLI's own process, where it must find
what it looks for, so "absent" inside cannot pass by the probe looking at the
wrong thing. Two more facts only a real namespace shows: a Bash child's stdin
is `/dev/null`, not the CLI's MCP stream, and the control dir, under the
NATIVE plan's `/tmp` tmpfs here, is readable through its late bind.

The leaking-launcher case is the negative control kept in the file: the real
launcher with `execve` handed `os.environ` merged into the env file's mapping.
The probes must find the sentinel there. (Removing `os.environ.clear()` alone
would not leak, because `execve` is passed the env file explicitly;
`tests/test_tool_launcher.py` holds the clear.)

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

import istota.executor as executor
from istota import db
from istota.brain import _tool_harness
from istota.brain._types import BrainRequest, HarnessSpec
from istota.brain.claude_code import ClaudeCodeBrain
from istota.config import SecurityConfig, UserConfig
from istota.executor import (
    SandboxProfile,
    _bwrap_available,
    build_bwrap_cmd,
    ensure_task_control_dir,
    ensure_task_harness_dir,
)
from istota.sandbox.skill_proxy import SkillProxy

from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux

FAKE_CLI = Path(__file__).resolve().parent.parent / "support" / "fake_claude_cli.py"
WORKSPACE_SENTINEL = "harness-workspace-sentinel"
PROXY_SECRET = "proxy-served-value"

# The real launcher with its `execve` given the launcher's own environment
# under the env file's: what a launcher that forgot to drop the CLI's
# environment would hand the tool server.
LEAKY_LAUNCHER = (
    "import os, sys\n"
    "from istota.sandbox import tool_launcher\n"
    "inherited = dict(os.environ)\n"
    "real_execve = os.execve\n"
    "os.execve = lambda path, argv, env: real_execve(path, argv, {**inherited, **env})\n"
    "sys.exit(tool_launcher.main())\n"
)


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _pattern(value: str) -> str:
    """A grep pattern matching ``value`` whose own text does not contain it,
    so a probe's command line (in `ps`, in `/proc/*/cmdline`) never counts."""
    return f"{value[:-1]}[{value[-1]}]"


def probe(sentinel: str, canary: str, sock: Path, sentinel_path: Path, python: str) -> str:
    """One `KEY=value` line per question. The same script runs from the CLI's
    own process (`host_probe`) and through the model's Bash in the tool server."""
    pat = _pattern(sentinel)
    # The canary is on disk where both sides can read it: what the filesystem
    # search must find, so an empty answer for the sentinel means something.
    canary_pat = _pattern(canary)
    connect = (
        "import json, socket, sys; s = socket.socket(socket.AF_UNIX); s.settimeout(10); "
        "s.connect(sys.argv[1]); "
        "s.sendall(json.dumps({'type': 'credential', 'name': 'GITLAB_TOKEN'}).encode() + b'\\n'); "
        "data = b''.join(iter(lambda: s.recv(65536), b'')); "
        "r = json.loads(data or b'{}'); "
        "print(r.get('reason') or ('SERVED' if r.get('value') == sys.argv[2] else 'OTHER'))"
    )
    return "; ".join([
        f'echo "ENV=$(env | grep -c \'{pat}\')"',
        f'echo "PROC=$(cat /proc/[0-9]*/environ 2>/dev/null | tr "\\0" "\\n" | grep -c \'{pat}\')"',
        f'echo "PS=$(ps axeww 2>/dev/null | grep -c \'{pat}\')"',
        # The whole visible tree but the kernel's own views.
        f'echo "FS=$(grep -rlsI --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev '
        f'-e \'{pat}\' / 2>/dev/null | head -5 | tr "\\n" " ")"',
        f'echo "FS_CANARY=$(grep -rlsI --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev '
        f'-e \'{canary_pat}\' / 2>/dev/null | head -5 | tr "\\n" " ")"',
        'if [ -d "{harness_dir}" ]; then echo HDIR=PRESENT; else echo HDIR=MISSING; fi',
        'if [ -e "{harness_dir}/pid.fifo" ]; then echo FIFO=PRESENT; else echo FIFO=MISSING; fi',
        'if [ -d "{harness_dir}/.." ]; then echo ROOT=PRESENT; else echo ROOT=MISSING; fi',
        f'echo "PROXY=$({python} -c "{connect}" {sock} {PROXY_SECRET} 2>&1 | tail -1)"',
        f'if [ -r {sentinel_path} ]; then echo "WS=$(cat {sentinel_path})"; else echo WS=MISSING; fi',
        'echo "STDIN=$(readlink /proc/self/fd/0)"',
        'echo "SERVER_STDIN=$(readlink /proc/$PPID/fd/0)"',
        'echo "SERVER_STDOUT=$(readlink /proc/$PPID/fd/1)"',
        'echo "SERVER_STDERR=$(readlink /proc/$PPID/fd/2)"',
    ])


def _comm_once_execed(pid: int, expected: str = "bwrap", deadline_seconds: float = 10.0) -> str:
    """The launcher reports its pid before it execs, so a read on arrival can
    still see `python`. The same pid must become ``expected``."""
    comm = "?"
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return comm
        if comm == expected:
            return comm
        time.sleep(0.01)
    return comm


def _section(text: str, start: str, end: str | None = None) -> dict[str, str]:
    body = text.split(start, 1)[1]
    if end is not None:
        body = body.split(end, 1)[0]
    answers = {}
    for line in body.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            answers.setdefault(key.strip(), value.strip())
    return answers


@pytest.fixture
def layout(tmp_path, make_config):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    temp = tmp_path / "temp"
    (temp / "alice").mkdir(parents=True)
    (temp / "alice" / "sentinel.txt").write_text(WORKSPACE_SENTINEL + "\n")
    data = tmp_path / "data"
    data.mkdir()
    return make_config(
        db_path=data / "istota.db",
        workspace_path=mount,
        temp_dir=temp,
        security=SecurityConfig(sandbox_enabled=True),
        users={"alice": UserConfig()},
    )


def _attempt(tmp_path, layout, monkeypatch, *, leaky: bool = False) -> tuple:
    user_temp = layout.temp_dir / "alice"
    task = db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )
    control_dir = ensure_task_control_dir(layout, "alice", 1)
    sentinel = "hcred-" + secrets.token_hex(12)
    canary = "hcanary-" + secrets.token_hex(12)
    (user_temp / "canary.txt").write_text(canary + "\n")

    # The proxy lives in a directory of its own, bound into the namespace as
    # a task's is, so the probe from inside reaches it at the same path.
    sock_dir = tmp_path / "proxy"
    sock_dir.mkdir(mode=0o700)
    sock = sock_dir / "skill.sock"
    proxy = SkillProxy(
        sock, {"GITLAB_TOKEN": PROXY_SECRET}, {"PATH": "/usr/bin:/bin"},
        task_id=1, trusted_roots=(),
    )
    proxy.start()

    wrapped: list[list[str]] = []

    def native_wrap(cmd):
        argv = build_bwrap_cmd(
            cmd, layout, task, False, [], user_temp, proxy_sock=sock,
            extra_ro_binds=[control_dir], profile=SandboxProfile.NATIVE,
        )
        assert argv[0] == "bwrap", "sandbox unavailable: every absence would pass for the wrong reason"
        wrapped.append(argv)
        return argv

    if leaky:
        launcher = tmp_path / "leaky_launcher.py"
        launcher.write_text(LEAKY_LAUNCHER)
        write_config = _tool_harness.write_mcp_config

        def leaky_config(path, **kw):
            write_config(path, **kw)
            document = json.loads(path.read_text())
            entry = document["mcpServers"][_tool_harness.HARNESS_MCP_SERVER]
            assert entry["args"][:3] == ["-I", "-m", "istota.sandbox.tool_launcher"]
            entry["args"] = ["-I", str(launcher), *entry["args"][3:]]
            path.write_text(json.dumps(document))

        monkeypatch.setattr(_tool_harness, "write_mcp_config", leaky_config)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    script = probe(sentinel, canary, sock, user_temp / "sentinel.txt",
                   str(executor.sandbox_interpreter()))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "host_probe": script,
        "calls": [{"name": "Bash", "args": {"command": "echo INSIDE:; " + script,
                                            "timeout": 600000}}],
    }))

    pids: list[int] = []
    comms: list[str] = []

    def on_pid(pid):
        pids.append(pid)
        if pid:
            # What `_record_worker_pid` does with it: the proxies authorize it.
            proxy.authorize_pid(pid)
            comms.append(_comm_once_execed(pid))

    path = os.environ.get("PATH", "/usr/bin:/bin")
    req = BrainRequest(
        prompt="probe",
        allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash"],
        cwd=user_temp,
        env={"PATH": "/usr/bin:/bin", "HOME": str(user_temp), "ISTOTA_DEFERRED_DIR": str(user_temp)},
        timeout_seconds=900,
        task_id=1,
        attempt=1,
        model="claude-sonnet-4-5",
        on_pid=on_pid,
        tool_server_wrap=native_wrap,
        fs_read_roots=[user_temp],
        fs_write_roots=[user_temp],
        harness=HarnessSpec(
            cli_env={
                "PATH": f"{bin_dir}:{path}", "FAKE_CLAUDE_PLAN": str(plan),
                "ISTOTA_CLI_AUTH": "api_key",
                # The CLI's credential, and a variable no name list knows.
                "CLAUDE_CODE_OAUTH_TOKEN": sentinel,
                "ISTOTA_TEST_HARNESS_SENTINEL": sentinel,
            },
            harness_root=ensure_task_harness_dir(layout, "alice", 1),
            control_dir=control_dir,
        ),
    )
    try:
        result = ClaudeCodeBrain().execute(req)
    finally:
        proxy.stop()
    return result, wrapped, pids, comms, control_dir


def test_a_tool_call_reaches_neither_the_clis_credential_nor_its_files(tmp_path, layout, monkeypatch):
    result, wrapped, pids, comms, control_dir = _attempt(tmp_path, layout, monkeypatch)

    assert result.success, result.result_text
    assert len(wrapped) == 1
    assert comms == ["bwrap"]
    assert pids[-1] == 0
    argv = wrapped[0]
    binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == "--ro-bind"]
    assert [str(control_dir), str(control_dir)] in binds

    outside = _section(result.result_text, "HOST:", "INSIDE:")
    inside = _section(result.result_text, "INSIDE:")
    # The CLI's own view: its environment holds the sentinel (twice), its
    # /proc shows it, the harness dir and FIFO are there, and the proxy
    # refuses it: the CLI is never a root.
    assert outside["ENV"] == "2", result.result_text
    assert int(outside["PROC"]) >= 2, result.result_text
    assert int(outside["PS"]) >= 1, result.result_text
    assert outside["HDIR"] == "PRESENT" and outside["FIFO"] == "PRESENT", result.result_text
    assert outside["ROOT"] == "PRESENT"
    assert outside["PROXY"] == "peer_not_in_task", result.result_text

    # The tool server's view: none of it.
    assert inside["ENV"] == "0", result.result_text
    assert inside["PROC"] == "0", result.result_text
    assert inside["PS"] == "0", result.result_text
    assert inside["FS"] == "", result.result_text
    assert str(layout.temp_dir / "alice" / "canary.txt") in inside["FS_CANARY"], result.result_text
    assert outside["FS"] == "", result.result_text
    assert inside["HDIR"] == "MISSING", result.result_text
    assert inside["FIFO"] == "MISSING", result.result_text
    assert inside["ROOT"] == "MISSING", result.result_text
    # Positive controls: the same call reads the task's own workspace, and the
    # proxy serves the tool server's tree, which `on_pid` authorized.
    assert inside["WS"] == WORKSPACE_SENTINEL, result.result_text
    assert inside["PROXY"] == "SERVED", result.result_text
    # The MCP stream is not on the server's fds 0 and 1, so a Bash child
    # inherits /dev/null as its stdin rather than the CLI's pipe.
    assert inside["STDIN"] == "/dev/null", result.result_text
    assert inside["SERVER_STDIN"] == "/dev/null", result.result_text
    # fd 1 is a copy of stderr (`tool_server` moves the stream off 0 and 1).
    assert inside["SERVER_STDOUT"] == inside["SERVER_STDERR"], result.result_text


def test_a_launcher_that_leaks_the_clis_environment_is_seen(tmp_path, layout, monkeypatch):
    """The negative control, kept: the same probes find the sentinel when the
    launcher hands the CLI's environment through."""
    result, _wrapped, _pids, comms, _control = _attempt(tmp_path, layout, monkeypatch, leaky=True)

    assert result.success, result.result_text
    assert comms == ["bwrap"]
    inside = _section(result.result_text, "INSIDE:")
    # Bash's own env is the hello's `subprocess_env` either way; the leak is
    # in the tool server's process, which `/proc` and `ps` show.
    assert int(inside["PROC"]) >= 1, result.result_text
    assert int(inside["PS"]) >= 1, result.result_text


def test_doctors_probe_runs_through_a_control_dir_on_the_sandboxs_tmp(tmp_path, layout, monkeypatch):
    """`runtime.model_execution` binds a control dir inside its private
    `TemporaryDirectory`, under `/tmp`, where the NATIVE plan mounts a tmpfs.
    The late `--ro-bind` lands on that tmpfs; the probe passes only when the
    tool server read its hello there and ran the Bash call."""
    from istota import doctor
    from istota.llm.providers import resolve_process_auth

    scratch = tmp_path / "fake-cli"
    scratch.mkdir()
    fake = scratch / "fake_claude_cli.py"
    shutil.copy(FAKE_CLI, fake)
    # The CLI's environment is the harness's allowlist, so the plan sits beside
    # the fake rather than in a variable.
    (scratch / "plan.json").write_text(json.dumps({
        "calls": [{"name": "Bash", "args": {"command": f"echo {doctor._MODEL_PROBE_MARKER}"}}],
    }))
    bin_dir = tmp_path / "doctor-bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setattr(tempfile, "tempdir", None)

    layout.brain.claude_code.auth = "api_key"
    layout.providers["anthropic"].api_key = "not-a-real-key"
    resolve_process_auth(layout, {})

    # Doctor imports `build_bwrap_cmd` from executor at call time.
    seen: list[list[str]] = []
    original = executor.build_bwrap_cmd

    def recording(*args, **kwargs):
        argv = original(*args, **kwargs)
        seen.append(argv)
        return argv

    monkeypatch.setattr(executor, "build_bwrap_cmd", recording)

    check = doctor.check_model_execution(layout, probe=True)

    assert check.status == doctor.OK, check
    assert len(seen) == 1 and seen[0][0] == "bwrap"
    argv = seen[0]
    binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == "--ro-bind"]
    controls = [b for b in binds if b[0].startswith("/tmp/istota-doctor-") and b[0].endswith("/control")]
    assert controls, binds
    tmpfs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--tmpfs"]
    assert "/tmp" in tmpfs, argv
