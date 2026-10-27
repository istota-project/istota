"""The pinned CLI on the tool harness, in the built image.

The CLI-brain spec runs `claude` outside the sandbox with every built-in that
acts on the machine switched off and its tools served by one MCP server. Each
case here runs one real `ClaudeCodeBrain` attempt in the image
(`cli_harness_run.py`): the real argv from `build_claude_cli_flags`, the real
adapter, bridge and tool server, the pinned CLI, and a bogus credential. The
CLI emits `system/init` before its first API request, then ends on "Invalid
API key", so no tool call runs and three things are read:

- the init frame lists exactly the harness built-ins plus
  `mcp__istota__<name>` for the six offered tools, and one MCP server,
  `istota`, connected, and the brain's own init check passed;
- under `strace`, the unwrapped CLI and the adapter it spawns open nothing
  under their cwd, which is the tool server's cwd and so the user's workspace,
  beyond stat-family calls;
- nothing appears under `/tmp`: the CLI's `claude-<uid>/` and `cc-socks/`
  follow the per-harness `TMPDIR`.

strace is not in the shipped image; `strace.Dockerfile` adds it on top, for
this file only.

Negative controls, `scripts/test-image-negative-control.sh cli-harness`, each
through `ISTOTA_TEST_CLI_HARNESS_CONTROL` (an `ISTOTA_TEST_*` name, which the
suite's env scrub keeps):

- `drop-tools`: `--tools` out of the argv. The deny list alone leaves
  CronCreate, SendMessage, ToolSearch and others in the frame; both init cases
  go red.
- `open-cwd`: a `CLAUDE.md` in the cwd, `--setting-sources user` and
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS` dropped; for the cwd case once it is green.
  It is an expected failure today (see its marker), so the script runs no
  control for it yet.
- `tmp-unset`: no `TMPDIR` on the CLI's env; the `/tmp` case goes red.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from .conftest import BuiltImage, assert_ok, build_image, run_in

pytestmark = pytest.mark.image

# `ISTOTA_TEST_*` is a prefix the suite's env scrub keeps (`tests/support/env_isolation.py`).
CONTROL_ENV = "ISTOTA_TEST_CLI_HARNESS_CONTROL"

HERE = Path(__file__).resolve().parent
MOUNT = "/stub"
DRIVER = "cli_harness_run.py"

MCP_SERVER = "istota"
OFFERED = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")

# Calls that look at a path without opening what is there.
STAT_LIKE = frozenset({
    "stat", "lstat", "fstat", "newfstatat", "fstatat64", "statx", "access",
    "faccessat", "faccessat2", "readlink", "readlinkat", "statfs", "getcwd",
    "chdir",
})
CREATING = frozenset({
    "mkdir", "mkdirat", "creat", "bind", "symlink", "symlinkat", "link",
    "linkat", "rename", "renameat", "renameat2", "mknod", "mknodat",
})

_CALL = re.compile(r"^\d+\s+(\w+)\((.*)$")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


@pytest.fixture(scope="session")
def strace_image(istota_image) -> BuiltImage:
    return build_image(
        HERE / "strace.Dockerfile", HERE,
        platform=istota_image.platform, prefix="istota-strace",
        build_args={"BASE": istota_image.tag},
    )


_RUNS: dict[tuple[str, str], dict] = {}


def _run(image: BuiltImage, builtins: tuple[str, ...]) -> dict:
    control = os.environ.get(CONTROL_ENV, "")
    key = (",".join(builtins), control)
    if key in _RUNS:
        return _RUNS[key]
    script = (
        "set -eu; "
        "setpriv --reuid=10001 --regid=10001 --clear-groups -- "
        f"python3 {MOUNT}/{DRIVER}"
    )
    result = run_in(
        image,
        ["-c", script],
        env={"HARNESS_BUILTINS": key[0], "HARNESS_CONTROL": control},
        volumes=[(HERE, MOUNT, "ro")],
    )
    out = assert_ok(result, "the harness driver")
    report = json.loads(out.strip().splitlines()[-1])
    _RUNS[key] = report
    return report


def _expected_tools(builtins: tuple[str, ...]) -> list[str]:
    return sorted([*builtins, *(f"mcp__{MCP_SERVER}__{name}" for name in OFFERED)])


def _calls(report: dict):
    """`(syscall, first path, line)` for each strace line the driver kept."""
    for line in report["strace"]:
        match = _CALL.match(line)
        if match is None:
            continue  # a `<... resumed>` tail; its path was on the first half
        paths = _QUOTED.findall(match.group(2))
        if paths:
            yield match.group(1), paths[0], line


def _assert_init(report: dict, builtins: tuple[str, ...]) -> None:
    frame = report["init"]
    assert frame is not None, report
    assert sorted(frame.get("tools") or []) == _expected_tools(builtins), frame.get("tools")
    servers = [(s.get("name"), s.get("status")) for s in frame.get("mcp_servers") or []]
    assert servers == [(MCP_SERVER, "connected")]
    # The brain's own check passed and the attempt ended on the bogus key.
    assert "did not load the tool set" not in report["result_text"], report["result_text"]
    assert report["pids"] and report["pids"][-1] == 0


class TestTheInitFrameListsOnlyWhatTheHarnessAskedFor:
    def test_websearch_granted(self, strace_image):
        _assert_init(_run(strace_image, ("WebSearch",)), ("WebSearch",))

    def test_no_builtins_granted(self, strace_image):
        """A guest turn has no WebSearch; `--tools ""` is the CLI's "no built-ins"."""
        _assert_init(_run(strace_image, ()), ())


class TestWhatTheUnwrappedCliTouches:
    # Red on 2.1.296, and left red on purpose until the spec decides (Stage 7
    # stop condition): at start-up the unwrapped CLI runs `git` three times in
    # its cwd (one `git -C <cwd> ls-files`), lists the whole cwd with
    # `rg --files --hidden`, puts an inotify watch on it, opens `.claude/` as
    # a directory and reads `.claude/scheduled_tasks.json` (the last goes with
    # CLAUDE_CODE_DISABLE_CRON=1). The cwd is the tool server's, so all of it
    # runs outside the sandbox over a tree the model can write. Strict, so the
    # decision that clears it turns this red and the marker comes off.
    @pytest.mark.xfail(
        strict=True,
        reason="the unwrapped CLI runs git and rg in its cwd; awaiting the spec's decision",
    )
    def test_it_opens_nothing_under_its_cwd(self, strace_image):
        report = _run(strace_image, ("WebSearch",))
        assert report["strace_lines"] > 0, "strace recorded nothing; the check would pass on no data"
        cwd = report["cwd"]
        opened = []
        stats = 0
        for syscall, path, line in _calls(report):
            under = path == cwd or path.startswith(cwd + "/") or (
                not path.startswith("/") and ("AT_FDCWD" in line or not syscall.endswith("at"))
            )
            if not under:
                continue
            # An `O_PATH` open reads nothing: it is how the CLI stats.
            if syscall in STAT_LIKE or (syscall == "openat" and "O_PATH" in line):
                stats += 1
            else:
                opened.append(line)
        assert opened == [], "\n".join(opened)
        # The CLI does look at its cwd; the check is that it only looks.
        assert stats > 0, "no stat under the cwd either: is the strace filter seeing the CLI?"

    def test_nothing_appears_under_tmp(self, strace_image):
        report = _run(strace_image, ("WebSearch",))
        assert report["tmp_new"] == [], report["tmp_new"]
        created = [
            line for syscall, path, line in _calls(report)
            if path.startswith("/tmp/")
            and (syscall in CREATING or (syscall in ("open", "openat") and "O_CREAT" in line))
        ]
        assert created == [], "\n".join(created)
        assert report["harness_dirs"], "no harness directory was made"
