"""The pinned CLI on the tool harness, in the built image.

The CLI-brain specs run `claude` outside the sandbox with every built-in that
acts on the machine switched off and its tools served by MCP. Since #862 the
CLI spawns its own tool server: `python -I -m istota.sandbox.tool_launcher`
from the harness's `mcp_config.json`, which clears the CLI's environment,
reports its pid down a FIFO and execs the tool server with `--mcp --hello`;
with WebFetch granted a second server, `istota_web`, runs beside it. Each case
here runs one real `ClaudeCodeBrain` attempt in the image
(`cli_harness_run.py`): the real argv from `build_claude_cli_flags`, the real
harness, launcher and servers, the pinned CLI, and a bogus credential. The
CLI emits `system/init` before its first API request, then ends on "Invalid
API key", so no tool call runs and these are read:

- the init frame lists exactly the harness built-ins plus
  `mcp__istota__<name>` for the six offered tools (and
  `mcp__istota_web__WebFetch` with WebFetch), and the servers the harness
  wrote, each connected; the brain's own init check passed; the launcher's
  pid was reported and its reap after it;
- under `strace`, the unwrapped CLI runs no real `git`: every exec is the
  CLI, the launcher and the tool server it execs, the WebFetch server, the
  harness's stub `git` or the bundled rg; and in its cwd, which is the tool
  server's cwd and so the user's workspace, the CLI and every child but the
  tool server only stat, list (rg), and watch (inotify), with nothing opened
  under `.claude/`. The tool server is the sandboxed side, whose cwd that is
  by design;
- nothing appears under `/tmp`: the CLI's `claude-<uid>/` and `cc-socks/`
  follow the per-harness `TMPDIR`;
- under the run contract's seccomp grant, a direct caller's shape: the tool
  server is wrapped by `build_daemon_sandbox`'s real bwrap, reads its hello
  from the daemon control dir bound read-only, and the reported pid is
  bwrap's.

strace is not in the shipped image; `strace.Dockerfile` adds it on top, for
this file only.

Negative controls, `scripts/test-image-negative-control.sh cli-harness`, each
through `ISTOTA_TEST_CLI_HARNESS_CONTROL` (an `ISTOTA_TEST_*` name, which the
suite's env scrub keeps):

- `drop-tools`: `--tools` out of the argv. The deny list alone leaves
  SendMessage, ListAgents, EnterWorktree and others in the frame; the init
  cases go red.
- `skip-report`: a launcher that execs the tool server without reporting its
  pid. The harness keeps the gate shut and fails the attempt ("the tool
  server never reported its pid"); the init cases go red.
- `stub-off`: the stub `git` left off the CLI's PATH; the image's real git
  runs in the cwd and the cwd case goes red.
- `open-cwd`: a `CLAUDE.md` in the cwd, `--setting-sources user` and
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS` dropped. The driver supports it; the script
  does not run it.
- `tmp-unset`: no `TMPDIR` on the CLI's env; the `/tmp` case goes red.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from .conftest import REPO, BuiltImage, assert_ok, build_image, run_in

pytestmark = pytest.mark.image

# `ISTOTA_TEST_*` is a prefix the suite's env scrub keeps (`tests/support/env_isolation.py`).
CONTROL_ENV = "ISTOTA_TEST_CLI_HARNESS_CONTROL"

HERE = Path(__file__).resolve().parent
MOUNT = "/stub"
DRIVER = "cli_harness_run.py"

MCP_SERVER = "istota"
WEB_SERVER = "istota_web"
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


#: What bwrap needs from the run contract (`tests/image/test_upgrade.py`'s
#: `_RUN_CONTRACT`), and the capabilities `setpriv` needs to drop to 10001.
_BWRAP_GRANT = [
    "--security-opt", f"seccomp={REPO / 'docker' / 'istota' / 'seccomp-istota.json'}",
    "--security-opt", "apparmor=istota",
    "--security-opt", "systempaths=unconfined",
    "--security-opt", "no-new-privileges:true",
    "--cap-drop", "ALL",
    "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "SETPCAP",
]

_RUNS: dict[tuple, dict] = {}


def _run(
    image: BuiltImage, builtins: tuple[str, ...], *,
    web_fetch: bool = False, wrap: str = "", strace: bool = True,
) -> dict:
    control = os.environ.get(CONTROL_ENV, "")
    key = (",".join(builtins), web_fetch, wrap, strace, control)
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
        env={
            "HARNESS_BUILTINS": key[0],
            "HARNESS_WEB_FETCH": "1" if web_fetch else "",
            "HARNESS_WRAP": wrap,
            "HARNESS_STRACE": "1" if strace else "0",
            "HARNESS_CONTROL": control,
        },
        volumes=[(HERE, MOUNT, "ro")],
        docker_args=_BWRAP_GRANT if wrap == "bwrap" else None,
    )
    out = assert_ok(result, "the harness driver")
    report = json.loads(out.strip().splitlines()[-1])
    _RUNS[key] = report
    return report


def _expected_tools(builtins: tuple[str, ...], *, web_fetch: bool = False) -> list[str]:
    tools = [*builtins, *(f"mcp__{MCP_SERVER}__{name}" for name in OFFERED)]
    if web_fetch:
        tools.append(f"mcp__{WEB_SERVER}__WebFetch")
    return sorted(tools)


def _calls(report: dict):
    """`(syscall, first path, line)` for each strace line the driver kept."""
    for line in report["strace"]:
        match = _CALL.match(line)
        if match is None:
            continue  # a `<... resumed>` tail; its path was on the first half
        paths = _QUOTED.findall(match.group(2))
        if paths:
            yield match.group(1), paths[0], line


def _pid(line: str) -> int | None:
    match = re.match(r"^(\d+)\s", line)
    return int(match.group(1)) if match else None


def _execs(report: dict) -> list[tuple[int | None, str, bool, str]]:
    """`(pid, path, succeeded, line)` for each execve strace saw. A call left
    `<unfinished ...>` is counted as run: the check errs toward red."""
    found = []
    for syscall, path, line in _calls(report):
        if syscall != "execve":
            continue
        failed = re.search(r"\)\s+=\s+-1\s", line) is not None
        found.append((_pid(line), path, not failed, line))
    return found


CLAUDE_PACKAGE = "/node_modules/@anthropic-ai/claude-code/"


def _is_bundled_rg(path: str, line: str) -> bool:
    """The CLI's own ripgrep, never one from PATH. 2.1.296 execs its own
    `claude.exe` from the package with `rg` as argv[0]."""
    if CLAUDE_PACKAGE not in path:
        return False
    argv0 = re.search(r'execve\("[^"]*",\s*\["([^"]*)"', line)
    return bool(argv0) and argv0.group(1) == "rg"


_CLONED = re.compile(r"^(\d+)\s+.*\b(?:clone3?|v?fork)\b.*=\s+(\d+)\s*$")


def _rg_family(report: dict) -> set[int]:
    """The pids of the bundled rg and of every thread or child it cloned after
    its exec, in log order. rg walks the cwd from worker threads, which strace
    names by their own tids."""
    family: set[int] = set()
    for line in report["strace"]:
        pid = _pid(line)
        if pid is None:
            continue
        exec_call = _CALL.match(line)
        if exec_call and exec_call.group(1) == "execve":
            paths = _QUOTED.findall(exec_call.group(2))
            failed = re.search(r"\)\s+=\s+-1\s", line) is not None
            if paths and not failed and _is_bundled_rg(paths[0], line):
                family.add(pid)
            continue
        cloned = _CLONED.match(line)
        if cloned and pid in family:
            family.add(int(cloned.group(2)))
    return family


def _succeeded(line: str) -> bool:
    return re.search(r"\)\s+=\s+-1\s", line) is None


def _server_family(report: dict) -> set[int]:
    """The tool server: the reported pid from the moment the launcher exec'd
    `istota.sandbox.tool_server`, and every thread or child it cloned after.
    The launcher's own life before that exec (same pid) is not included: it
    runs as the CLI's child, before any sandbox, and is held to the CLI's rule.
    """
    reported = [pid for pid in report["pids"] if pid]
    if not reported:
        return set()
    server = reported[0]
    family: set[int] = set()
    for line in report["strace"]:
        pid = _pid(line)
        if pid is None:
            continue
        call = _CALL.match(line)
        if (
            pid == server and call and call.group(1) == "execve"
            and "istota.sandbox.tool_server" in line and _succeeded(line)
        ):
            family.add(pid)
            continue
        cloned = _CLONED.match(line)
        if cloned and pid in family:
            family.add(int(cloned.group(2)))
    return family


def _assert_init(report: dict, builtins: tuple[str, ...], *, web_fetch: bool = False) -> None:
    # The brain's own checks passed and the attempt ended on the bogus key:
    # the init check, and the harness's (a reported, authorized tool server).
    assert "did not load the tool set" not in report["result_text"], report["result_text"]
    assert "never reported its pid" not in report["result_text"], report["result_text"]
    frame = report["init"]
    assert frame is not None, report
    assert sorted(frame.get("tools") or []) == _expected_tools(builtins, web_fetch=web_fetch), (
        frame.get("tools")
    )
    servers = sorted((s.get("name"), s.get("status")) for s in frame.get("mcp_servers") or [])
    expected = [(MCP_SERVER, "connected")] + ([(WEB_SERVER, "connected")] if web_fetch else [])
    assert servers == sorted(expected)
    # One tool server reported, then its reap; never the CLI, never istota_web.
    assert len(report["pids"]) == 2 and report["pids"][0] > 0 and report["pids"][1] == 0, (
        report["pids"]
    )
    # Close removed every file the harness wrote.
    assert report["harness_files"] == [], report["harness_files"]


class TestTheInitFrameListsOnlyWhatTheHarnessAskedFor:
    def test_websearch_granted(self, strace_image):
        _assert_init(_run(strace_image, ("WebSearch",)), ("WebSearch",))

    def test_no_builtins_granted(self, strace_image):
        """A guest turn has no WebSearch; `--tools ""` is the CLI's "no built-ins"."""
        _assert_init(_run(strace_image, ()), ())

    def test_webfetch_granted_is_a_second_server(self, strace_image):
        """The pinned CLI accepts two servers and names the second one's tool
        `mcp__istota_web__WebFetch`, which the init check expects."""
        report = _run(strace_image, ("WebSearch",), web_fetch=True)
        _assert_init(report, ("WebSearch",), web_fetch=True)


class TestWhatTheUnwrappedCliTouches:
    # On 2.1.296 the unwrapped CLI runs `git` three times in its cwd at
    # start-up, lists the cwd with its bundled `rg --files --hidden`, puts an
    # inotify watch on it, and with cron on reads `.claude/scheduled_tasks.json`.
    # The cwd is the tool server's, a tree the model can write, so the harness
    # puts a stub `git` (exit 128) first on the CLI's PATH and sets
    # CLAUDE_CODE_DISABLE_CRON=1. This case holds the CLI to that: every exec
    # is the CLI, the launcher, the tool server it execs, the WebFetch server,
    # the stub or the bundled rg, and in the cwd everything but the tool server
    # only stats, lists (rg) and watches. The run grants WebFetch, so the
    # unwrapped WebFetch server is held to the CLI's rule too.
    def test_it_runs_no_real_git_and_only_looks_at_its_cwd(self, strace_image):
        report = _run(strace_image, ("WebSearch",), web_fetch=True)
        assert report["strace_lines"] > 0, "strace recorded nothing; the check would pass on no data"
        cwd = report["cwd"]
        assert len(report["harness_dirs"]) == 1, report["harness_dirs"]
        stub = f"{report['harness_dirs'][0]}/bin/git"
        pythons = {report["python"], report["server_python"]}

        execs = _execs(report)
        shown = "\n".join(line for _, _, _, line in execs)
        assert not [e for e in execs if e[1] != stub and os.path.basename(e[1]) == "git"], (
            "a git other than the harness stub was run:\n" + shown
        )
        ran = [e for e in execs if e[2]]
        assert any(path == stub for _, path, _, _ in ran), "the stub was never run:\n" + shown
        rg_pids = _rg_family(report)
        assert rg_pids, "the bundled rg was never run; the allowlist below would be vacuous:\n" + shown
        server_pids = _server_family(report)
        assert server_pids, "the tool server's exec was not seen:\n" + shown
        for module in ("istota.sandbox.tool_launcher", "istota.brain._web_fetch_mcp"):
            assert any(path in pythons and module in line for _, path, _, line in ran), (
                f"{module} was never run:\n" + shown
            )
        unexpected = [
            line for pid, path, _, line in ran
            if not (
                path == stub
                or _is_bundled_rg(path, line)
                or path == report["real_claude"]
                or (path in pythons and (
                    "istota.sandbox.tool_launcher" in line
                    or "istota.sandbox.tool_server" in line
                    or "istota.brain._web_fetch_mcp" in line
                ))
            )
            # What the tool server itself runs is the model's Bash, in the
            # sandbox on a real deployment; no call runs here anyway.
            and pid not in server_pids
        ]
        assert unexpected == [], "\n".join(unexpected)

        opened = []
        stats = 0
        for syscall, path, line in _calls(report):
            if syscall == "execve" or _pid(line) in server_pids:
                continue
            under = path == cwd or path.startswith(cwd + "/") or (
                not path.startswith("/") and ("AT_FDCWD" in line or not syscall.endswith("at"))
            )
            if not under:
                continue
            if path == f"{cwd}/.claude" or path.startswith(f"{cwd}/.claude/"):
                # The CLI tries `.claude` itself as a directory (a listing at
                # most); nothing below it may be opened.
                directory = path == f"{cwd}/.claude" and "O_DIRECTORY" in line
                if not (syscall in STAT_LIKE or "O_PATH" in line or directory):
                    opened.append(line)
                continue
            # An `O_PATH` open reads nothing: it is how the CLI stats.
            if syscall in STAT_LIKE or (syscall == "openat" and "O_PATH" in line):
                stats += 1
            elif syscall == "inotify_add_watch":
                continue
            elif _pid(line) in rg_pids and "O_DIRECTORY" in line:
                continue
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


class TestTheToolServerUnderBwrap:
    """A direct caller's shape (health OCR, the code reviewer) under the run
    contract's seccomp grant: `build_daemon_sandbox`'s NATIVE wrap around the
    tool server, the CLI spawning the launcher, the launcher exec'ing a real
    bwrap, the server reading its hello and gate from the daemon control dir
    bound read-only. No strace here: the question is whether the wrapped
    server comes up for the pinned CLI, not what the CLI touches."""

    def test_the_wrapped_server_connects_and_bwrap_is_what_reports(self, istota_image):
        report = _run(istota_image, ("WebSearch",), web_fetch=True, wrap="bwrap", strace=False)
        _assert_init(report, ("WebSearch",), web_fetch=True)
        assert report["comms"] == ["bwrap"], report["comms"]
        assert len(report["wrapped"]) == 1, report["wrapped"]
        argv = report["wrapped"][0]
        control = report["control_dir"]
        assert control and "--ro-bind" in argv
        binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == "--ro-bind"]
        assert [control, control] in binds, argv
        assert "--die-with-parent" in argv
        assert any("istota.sandbox.tool_server" in arg for arg in argv)
        # release_daemon_sandbox removed the control dir with the hello in it.
        assert report["control_dir_left"] is False
