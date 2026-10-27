"""The pinned CLI's `system/init` under the harness argv, in the built image.

The CLI-brain spec runs `claude` outside the sandbox with every built-in that
acts on the machine switched off and its tools served by one MCP server. Its
fail-closed rule reads `system/init` and kills the attempt when the frame lists
anything the harness did not ask for. This holds the pinned CLI to that frame:
with the harness argv, a stub adapter and a bogus credential, `system/init`
lists exactly the harness built-ins plus `mcp__istota__<name>` for the six
offered tools, and exactly one MCP server, `istota`, connected.

A bogus credential is enough because the CLI emits `system/init` before its
first API request; the run then ends with "Invalid API key" and no tool call.

The argv is spelled out here rather than taken from `build_claude_cli_flags`,
whose harness branch arrives in Stage 6 of the spec; Stage 6 points this file
at it, and at the real adapter in place of `cli_harness_stub_adapter.py`.

Negative control: `scripts/test-image-negative-control.sh cli-harness`, which
sets `ISTOTA_TEST_CLI_HARNESS_CONTROL=drop-tools`. Without `--tools` the deny list
alone leaves CronCreate, SendMessage, ToolSearch and others in the frame, and
both tests below must go red.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from .conftest import assert_ok, run_in

pytestmark = pytest.mark.image

# `ISTOTA_TEST_*` is a prefix the suite's env scrub keeps (`tests/support/env_isolation.py`).
CONTROL_ENV = "ISTOTA_TEST_CLI_HARNESS_CONTROL"

STUB_DIR = Path(__file__).resolve().parent
STUB_NAME = "cli_harness_stub_adapter.py"
STUB_MOUNT = "/stub"

MCP_SERVER = "istota"
OFFERED = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")

# The spec's `HARNESS_DENIED_TOOLS`, plus Agent and Workflow from today's argv.
DENIED = (
    "Agent", "Workflow", "Bash", "BashOutput", "KillShell", "Monitor", "Read",
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
    "WebFetch", "Skill", "TodoWrite",
)


def _harness_argv(builtins: tuple[str, ...], mcp_config: str) -> list[str]:
    argv = ["claude", "-p", "-", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions", "--setting-sources", "user",
            "--settings", json.dumps({"attribution": {"commit": "", "pr": ""}})]
    if os.environ.get(CONTROL_ENV) != "drop-tools":
        argv += ["--tools", ",".join(builtins)]
    argv += ["--mcp-config", mcp_config, "--strict-mcp-config",
             "--disallowedTools", *DENIED]
    return argv


def _init_frame(istota_image, builtins: tuple[str, ...]) -> dict:
    config = {"mcpServers": {MCP_SERVER: {
        "type": "stdio",
        "command": "python3",
        "args": ["-I", f"{STUB_MOUNT}/{STUB_NAME}"],
        "env": {},
    }}}
    script = "\n".join([
        "set -eu",
        "mkdir -p /tmp/h/config /tmp/h/work",
        f"printf '%s' {shlex.quote(json.dumps(config))} > /tmp/h/mcp_config.json",
        "cd /tmp/h/work",
        # `|| true`: the bogus key makes the run exit 1 after `system/init`.
        "echo 'Say OK.' | " + shlex.join(_harness_argv(builtins, "/tmp/h/mcp_config.json"))
        + " || true",
    ])
    result = run_in(
        istota_image,
        ["-c", script],
        env={
            "HOME": "/tmp/h",
            "CLAUDE_CONFIG_DIR": "/tmp/h/config",
            "IS_SANDBOX": "1",
            "ANTHROPIC_API_KEY": "not-a-real-key",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        volumes=[(STUB_DIR, STUB_MOUNT, "ro")],
    )
    out = assert_ok(result, "claude -p with the harness argv")
    for line in out.splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("type") == "system" and frame.get("subtype") == "init":
            return frame
    pytest.fail(f"no system/init frame in the CLI's output:\n{out}\n{result.stderr}")


def _expected_tools(builtins: tuple[str, ...]) -> list[str]:
    return sorted([*builtins, *(f"mcp__{MCP_SERVER}__{name}" for name in OFFERED)])


def _servers(frame: dict) -> list[tuple[str, str]]:
    return [(s.get("name"), s.get("status")) for s in frame.get("mcp_servers") or []]


class TestTheInitFrameListsOnlyWhatTheHarnessAskedFor:
    def test_websearch_granted(self, istota_image):
        frame = _init_frame(istota_image, ("WebSearch",))
        assert sorted(frame.get("tools") or []) == _expected_tools(("WebSearch",))
        assert _servers(frame) == [(MCP_SERVER, "connected")]

    def test_no_builtins_granted(self, istota_image):
        """A guest turn has no WebSearch; `--tools ""` is the CLI's "no built-ins"."""
        frame = _init_frame(istota_image, ())
        assert sorted(frame.get("tools") or []) == _expected_tools(())
        assert _servers(frame) == [(MCP_SERVER, "connected")]
