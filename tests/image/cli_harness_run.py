"""Run inside the istota image: one ClaudeCodeBrain attempt on the tool harness.

The real brain, harness, launcher and tool server, and the pinned `claude`,
with a bogus API key: the CLI spawns its MCP servers, prints `system/init`
before its first API request and then ends on "Invalid API key", so no tool
call runs and the frame is the whole subject. `claude` is reached through a
wrapper on PATH that runs it under `strace`, so the host-side test can read
what the unwrapped CLI and everything it spawns opened under their cwd and
created under `/tmp`.

Configured by environment:

- `HARNESS_BUILTINS`: `WebSearch` to grant it, empty for none.
- `HARNESS_WEB_FETCH`: `1` grants WebFetch with the policy on, so the CLI
  spawns the second MCP server, `istota_web`.
- `HARNESS_WRAP`: `bwrap` runs the tool server the way a direct caller does,
  under `build_daemon_sandbox`'s NATIVE wrap with its daemon control dir
  bound read-only (the container must carry the run contract's seccomp and
  `systempaths` grant). Empty runs it unwrapped.
- `HARNESS_STRACE`: `0` runs `claude` without strace.
- `HARNESS_CONTROL`: a negative control. `drop-tools` takes `--tools` out of
  the argv; `open-cwd` plants a `CLAUDE.md` in the cwd and drops
  `--setting-sources user` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS`; `tmp-unset`
  leaves `TMPDIR` off the CLI's env; `stub-off` leaves the harness's stub
  `git` off the CLI's PATH, so the image's real git runs in the cwd;
  `skip-report` runs a launcher that execs the tool server without reporting
  its pid.

The daemon's own temp dir is moved off `/tmp` first, so its files do not count
against the `/tmp` check. The last stdout line is a JSON report.
"""

import dataclasses
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

BASE = Path(os.environ.get("HARNESS_BASE", "/data/drv"))
CONTROL = os.environ.get("HARNESS_CONTROL", "")
BUILTINS = [t for t in os.environ.get("HARNESS_BUILTINS", "").split(",") if t]
WEB_FETCH = os.environ.get("HARNESS_WEB_FETCH", "") == "1"
WRAP = os.environ.get("HARNESS_WRAP", "")
STRACE = os.environ.get("HARNESS_STRACE", "1") != "0"
SIX = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
USER = "alice"

# The `skip-report` control's launcher: the real one with its report step
# replaced by nothing, so the tool server starts and no pid reaches the daemon.
NO_REPORT_LAUNCHER = (
    "import sys\n"
    "from istota.sandbox import tool_launcher\n"
    "tool_launcher._report_pid = lambda fifo: None\n"
    "sys.exit(tool_launcher.main())\n"
)


def _tmp_listing() -> list[str]:
    found = []
    for root, dirs, files in os.walk("/tmp"):
        for name in dirs + files:
            found.append(os.path.join(root, name))
    return sorted(found)


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "?"


def main() -> int:
    ws = BASE / "ws"
    root = BASE / "root"
    bin_dir = BASE / "bin"
    out = BASE / "out"
    daemon_tmp = BASE / "daemon-tmp"
    home = BASE / "home"
    temp = BASE / "temp"
    for path in (ws, root, bin_dir, out, daemon_tmp, home, temp, temp / USER):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    os.environ["TMPDIR"] = str(daemon_tmp)
    os.environ["HOME"] = str(home)
    tempfile.tempdir = None

    real_claude = shutil.which("claude")
    strace_log = out / "strace.log"
    wrapper = bin_dir / "claude"
    if STRACE:
        wrapper.write_text(
            "#!/bin/sh\n"
            f"exec strace -f -qq -o {strace_log} "
            "-e trace=%file,bind,connect,clone,clone3,fork,vfork "
            f"-- {real_claude} \"$@\"\n"
        )
    else:
        wrapper.write_text(f"#!/bin/sh\nexec {real_claude} \"$@\"\n")
    wrapper.chmod(stat.S_IRWXU)

    from istota.brain import _tool_harness, claude_code
    from istota.brain._types import BrainRequest, HarnessSpec
    from istota.config import Config, SecurityConfig
    from istota.executor import (
        SandboxProfile,
        build_daemon_harness,
        build_daemon_sandbox,
        build_harness_cli_env,
        release_daemon_sandbox,
    )
    from istota.llm.providers import resolve_process_auth
    from istota.session.tools import WebFetchPolicy

    config = Config(
        db_path=BASE / "istota.db",
        temp_dir=temp,
        security=SecurityConfig(sandbox_enabled=WRAP == "bwrap"),
    )
    config.brain.claude_code.auth = "api_key"
    config.providers["anthropic"].api_key = "not-a-real-key"
    resolve_process_auth(config, {})

    if CONTROL == "open-cwd":
        (ws / "CLAUDE.md").write_text("planted\n")

    flags = claude_code.build_claude_cli_flags

    def controlled_flags(req, **kw):
        argv = flags(req, **kw)
        if CONTROL == "drop-tools" and "--tools" in argv:
            index = argv.index("--tools")
            del argv[index:index + 2]
        if CONTROL == "open-cwd" and "--setting-sources" in argv:
            index = argv.index("--setting-sources")
            del argv[index:index + 2]
        return argv

    claude_code.build_claude_cli_flags = controlled_flags

    harness_env = claude_code._harness_cli_env

    def controlled_env(req, handle):
        env = harness_env(req, handle)
        if CONTROL == "tmp-unset":
            env.pop("TMPDIR", None)
        if CONTROL == "stub-off":
            parts = env["PATH"].split(os.pathsep)
            env["PATH"] = os.pathsep.join(p for p in parts if p != str(handle.bin_dir))
        if CONTROL == "open-cwd":
            env.pop("CLAUDE_CODE_DISABLE_CLAUDE_MDS", None)
        return env

    claude_code._harness_cli_env = controlled_env

    if CONTROL == "skip-report":
        launcher = bin_dir / "no_report_launcher.py"
        launcher.write_text(NO_REPORT_LAUNCHER)
        write_config = _tool_harness.write_mcp_config

        def controlled_config(path, **kw):
            write_config(path, **kw)
            document = json.loads(path.read_text())
            entry = document["mcpServers"][_tool_harness.HARNESS_MCP_SERVER]
            assert entry["args"][:3] == ["-I", "-m", "istota.sandbox.tool_launcher"]
            entry["args"] = ["-I", str(launcher), *entry["args"][3:]]
            path.write_text(json.dumps(document))

        _tool_harness.write_mcp_config = controlled_config

    frames: list[dict] = []
    parser_factory = claude_code.make_stream_parser

    def recording_parser():
        parse = parser_factory()

        def parse_line(line):
            try:
                frame = json.loads(line)
                if frame.get("type") == "system" and frame.get("subtype") == "init":
                    frames.append(frame)
            except ValueError:
                pass
            return parse(line)

        return parse_line

    claude_code.make_stream_parser = recording_parser

    pids: list[int] = []
    comms: list[str] = []

    def on_pid(pid):
        pids.append(pid)
        if pid:
            comms.append(_comm(pid))

    allowed = [*SIX, *BUILTINS] + (["WebFetch"] if WEB_FETCH else [])
    web_fetch = WebFetchPolicy() if WEB_FETCH else None
    wrapped: list[list[str]] = []
    sandbox = None
    if WRAP == "bwrap":
        # A direct caller's shape (health OCR, the code reviewer): the
        # per-user work dir as cwd, a daemon control dir bound read-only.
        sandbox = build_daemon_sandbox(config, USER, profile=SandboxProfile.NATIVE)
        assert sandbox.tool_server_wrap is not None, sandbox

        def wrap(cmd):
            argv = sandbox.tool_server_wrap(cmd)
            wrapped.append(argv)
            return argv

        harness = build_daemon_harness(
            config, USER, brain_kind="claude_code", timeout_seconds=120,
            harness_root=root, control_dir=sandbox.control_dir,
        )
        cli_env = dict(harness.cli_env)
        cli_env["PATH"] = f"{bin_dir}:{cli_env.get('PATH') or os.environ['PATH']}"
        harness = dataclasses.replace(harness, cli_env=cli_env, web_fetch=web_fetch)
        cwd = sandbox.work_dir
        req_env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "ISTOTA_DEFERRED_DIR": str(cwd)}
        roots = [cwd]
    else:
        cli_env = build_harness_cli_env(config, USER, timeout_seconds=120)
        cli_env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        harness = HarnessSpec(cli_env=cli_env, harness_root=root, web_fetch=web_fetch)
        wrap = None
        cwd = ws
        # The tool server runs unwrapped here, with no `/tmp` of its own as it
        # would have under bwrap; its temp files are not the CLI's.
        req_env = {"PATH": os.environ["PATH"], "HOME": str(ws), "ISTOTA_DEFERRED_DIR": str(ws),
                   "TMPDIR": str(daemon_tmp)}
        roots = None

    req = BrainRequest(
        prompt="Say OK.",
        allowed_tools=allowed,
        cwd=cwd,
        env=req_env,
        timeout_seconds=120,
        task_id=1,
        attempt=1,
        on_pid=on_pid,
        tool_server_wrap=wrap,
        fs_read_roots=roots,
        fs_write_roots=roots,
        harness=harness,
    )

    before = _tmp_listing()
    try:
        result = claude_code.ClaudeCodeBrain().execute(req)
    finally:
        if sandbox is not None:
            release_daemon_sandbox(sandbox)
    after = _tmp_listing()

    harness_dirs = sorted(str(p) for p in root.iterdir())
    lines = strace_log.read_text(errors="replace").splitlines() if strace_log.exists() else []
    from istota.session.tools.remote import server_command

    report = {
        "init": frames[0] if frames else None,
        "stop_reason": result.stop_reason,
        "result_text": (result.result_text or "")[:600],
        "cwd": str(cwd),
        "harness_dirs": harness_dirs,
        "harness_files": sorted(
            str(p.relative_to(root)) for p in root.rglob("*")
            if p.name in ("mcp_config.json", "tool-env.json", "pid.fifo", "web-fetch.json")
        ),
        "tmp_new": sorted(set(after) - set(before)),
        "strace_lines": len(lines),
        "real_claude": real_claude,
        "python": sys.executable,
        "server_python": server_command()[0],
        "wrapped": wrapped,
        "control_dir": str(sandbox.control_dir) if sandbox is not None else None,
        "control_dir_left": bool(sandbox is not None and os.path.lexists(sandbox.control_dir)),
        # Every exec, clone and inotify watch, and every line naming the cwd,
        # a relative path, or /tmp; the test decides.
        "strace": [
            line for line in lines
            if str(cwd) in line or "/tmp" in line
            or ('AT_FDCWD, "' in line and 'AT_FDCWD, "/' not in line)
            or "execve(" in line or "inotify_add_watch(" in line
            or "clone" in line or "fork" in line
        ],
        "pids": pids,
        "comms": comms,
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
