"""The CLI tool harness with its tool server under a real bwrap.

`tests/test_claude_code_harness.py` runs the same path with the tool server
unwrapped, on any platform. Here the tool server is wrapped by
`build_bwrap_cmd(..., profile=NATIVE)` beneath an unwrapped CLI (the fake in
`tests/support/fake_claude_cli.py`, which starts the real adapter), and the
question is what a tool call can reach: neither the bridge socket the adapter
connects to nor the harness directory holding the CLI's config dir.

The same probe runs twice, from the CLI's own view first and then through the
model's Bash in the tool server, so "absent" inside cannot pass by the probe
looking at the wrong path: outside, the socket and the directory are there. A
workspace sentinel read through the same Bash call is the positive control
that the namespace binds what it should.

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from istota import db
from istota.brain._types import BrainRequest, HarnessSpec
from istota.brain.claude_code import ClaudeCodeBrain
from istota.config import SecurityConfig
from istota.executor import (
    SandboxProfile,
    _bwrap_available,
    build_bwrap_cmd,
    ensure_task_harness_dir,
)

from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux

FAKE_CLI = Path(__file__).resolve().parent.parent / "support" / "fake_claude_cli.py"
SENTINEL = "harness-workspace-sentinel"


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def reach_probe(sentinel_path) -> str:
    """One line per question, printed on both arms."""
    return "; ".join([
        'if [ -e "{socket}" ]; then echo SOCK=PRESENT; else echo SOCK=ABSENT; fi',
        'if [ -d "{harness_dir}" ]; then echo "HDIR=[$(ls -A "{harness_dir}" 2>&1 | tr "\\n" " ")]"; '
        'else echo HDIR=MISSING; fi',
        'if [ -d "{harness_dir}/.." ]; then echo ROOT=PRESENT; else echo ROOT=MISSING; fi',
        f'if [ -r {shlex.quote(str(sentinel_path))} ]; then echo "WS=$(cat {shlex.quote(str(sentinel_path))})"; '
        'else echo WS=MISSING; fi',
    ])


def connect_probe() -> str:
    """Whether a process can connect to the bridge socket, by Python if the
    namespace has one, printed on every arm."""
    return (
        'if command -v python3 >/dev/null 2>&1; then '
        'python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); '
        "s.connect(sys.argv[1]); print('CONNECT=OK')\" \"{socket}\" 2>/dev/null || echo CONNECT=FAIL; "
        'else echo CONNECT=NOPYTHON; fi'
    )


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
    (temp / "alice" / "sentinel.txt").write_text(SENTINEL + "\n")
    data = tmp_path / "data"
    data.mkdir()
    return make_config(
        db_path=data / "istota.db",
        workspace_path=mount,
        temp_dir=temp,
        security=SecurityConfig(sandbox_enabled=True),
    )


def test_a_tool_call_reaches_neither_the_bridge_socket_nor_the_harness_dir(tmp_path, layout):
    user_temp = layout.temp_dir / "alice"
    task = db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )
    wrapped: list[list[str]] = []

    def native_wrap(cmd):
        argv = build_bwrap_cmd(cmd, layout, task, False, [], user_temp, profile=SandboxProfile.NATIVE)
        assert argv[0] == "bwrap", "sandbox unavailable: every absence would pass for the wrong reason"
        wrapped.append(argv)
        return argv

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    probe = reach_probe(user_temp / "sentinel.txt") + "; " + connect_probe()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "host_probe": probe,
        "calls": [{"name": "Bash", "args": {"command": "echo INSIDE:; " + probe}}],
    }))

    pid_comms: list[str] = []

    def on_pid(pid):
        if pid:
            try:
                pid_comms.append(Path(f"/proc/{pid}/comm").read_text().strip())
            except OSError:
                pid_comms.append("?")

    path = os.environ.get("PATH", "/usr/bin:/bin")
    req = BrainRequest(
        prompt="probe",
        allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash"],
        cwd=user_temp,
        env={"PATH": "/usr/bin:/bin", "HOME": str(user_temp), "ISTOTA_DEFERRED_DIR": str(user_temp)},
        timeout_seconds=120,
        task_id=1,
        attempt=1,
        model="claude-sonnet-4-5",
        on_pid=on_pid,
        native_sandbox_wrap=native_wrap,
        sandbox_wrap=lambda cmd: pytest.fail("the CLI was wrapped"),
        harness=HarnessSpec(
            cli_env={"PATH": f"{bin_dir}:{path}", "FAKE_CLAUDE_PLAN": str(plan),
                     "ISTOTA_CLI_AUTH": "api_key"},
            harness_root=ensure_task_harness_dir(layout, "alice", 1),
        ),
    )

    result = ClaudeCodeBrain().execute(req)

    assert result.success, result.result_text
    assert len(wrapped) == 1
    assert pid_comms == ["bwrap"]

    outside = _section(result.result_text, "HOST:", "INSIDE:")
    inside = _section(result.result_text, "INSIDE:")
    # The CLI's own view: the socket and the harness dir are there.
    assert outside["SOCK"] == "PRESENT", result.result_text
    assert "mcp_config.json" in outside["HDIR"], result.result_text
    assert outside["ROOT"] == "PRESENT"
    assert outside["CONNECT"] == "OK", result.result_text
    # The tool server's view: neither is.
    assert inside["SOCK"] == "ABSENT", result.result_text
    assert inside["CONNECT"] != "OK", result.result_text
    assert inside["HDIR"] == "MISSING", result.result_text
    assert inside["ROOT"] == "MISSING", result.result_text
    # Positive control: the same call reads the task's own workspace.
    assert inside["WS"] == SENTINEL, result.result_text
