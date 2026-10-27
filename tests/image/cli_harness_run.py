"""Run inside the istota image: one ClaudeCodeBrain attempt on the tool harness.

The real brain, adapter, bridge and tool server, and the pinned `claude`, with a
bogus API key: the CLI prints `system/init` before its first API request and
then ends on "Invalid API key", so no tool call runs and the frame is the whole
subject. `claude` is reached through a wrapper on PATH that runs it under
`strace`, so the host-side test can read what the unwrapped CLI and the
adapter it spawns opened under their cwd and created under `/tmp`.

Configured by environment:

- `HARNESS_BUILTINS`: `WebSearch` to grant it, empty for none.
- `HARNESS_CONTROL`: a negative control. `drop-tools` takes `--tools` out of
  the argv; `open-cwd` plants a `CLAUDE.md` in the cwd and drops
  `--setting-sources user` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS`; `tmp-unset`
  leaves `TMPDIR` off the CLI's env; `stub-off` leaves the harness's stub
  `git` off the CLI's PATH, so the image's real git runs in the cwd.

The daemon's own temp dir is moved off `/tmp` first, so the bridge socket does
not count against the `/tmp` check. The last stdout line is a JSON report.
"""

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
SIX = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


def _tmp_listing() -> list[str]:
    found = []
    for root, dirs, files in os.walk("/tmp"):
        for name in dirs + files:
            found.append(os.path.join(root, name))
    return sorted(found)


def main() -> int:
    ws = BASE / "ws"
    root = BASE / "root"
    bin_dir = BASE / "bin"
    out = BASE / "out"
    daemon_tmp = BASE / "daemon-tmp"
    home = BASE / "home"
    for path in (ws, root, bin_dir, out, daemon_tmp, home):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    os.environ["TMPDIR"] = str(daemon_tmp)
    os.environ["HOME"] = str(home)
    tempfile.tempdir = None

    real_claude = shutil.which("claude")
    strace_log = out / "strace.log"
    wrapper = bin_dir / "claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec strace -f -qq -o {strace_log} "
        "-e trace=%file,bind,connect,clone,clone3,fork,vfork "
        f"-- {real_claude} \"$@\"\n"
    )
    wrapper.chmod(stat.S_IRWXU)

    from istota.brain import claude_code
    from istota.brain._types import BrainRequest, HarnessSpec
    from istota.config import Config
    from istota.executor import build_harness_cli_env
    from istota.llm.providers import resolve_process_auth

    config = Config(db_path=BASE / "istota.db", temp_dir=BASE / "temp")
    config.brain.claude_code.auth = "api_key"
    config.providers["anthropic"].api_key = "not-a-real-key"
    resolve_process_auth(config, {})
    cli_env = build_harness_cli_env(config, "alice", timeout_seconds=120)
    cli_env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"

    if CONTROL == "open-cwd":
        (ws / "CLAUDE.md").write_text("planted\n")
        cli_env.pop("CLAUDE_CODE_DISABLE_CLAUDE_MDS", None)

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
        return env

    claude_code._harness_cli_env = controlled_env

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
    req = BrainRequest(
        prompt="Say OK.",
        allowed_tools=[*SIX, *BUILTINS],
        cwd=ws,
        # The tool server runs unwrapped here, with no `/tmp` of its own as it
        # would have under bwrap; its temp files are not the CLI's.
        env={"PATH": os.environ["PATH"], "HOME": str(ws), "ISTOTA_DEFERRED_DIR": str(ws),
             "TMPDIR": str(daemon_tmp)},
        timeout_seconds=120,
        task_id=1,
        attempt=1,
        on_pid=pids.append,
        harness=HarnessSpec(cli_env=cli_env, harness_root=root),
    )

    before = _tmp_listing()
    result = claude_code.ClaudeCodeBrain().execute(req)
    after = _tmp_listing()

    harness_dirs = sorted(str(p) for p in root.iterdir())
    lines = strace_log.read_text(errors="replace").splitlines() if strace_log.exists() else []
    report = {
        "init": frames[0] if frames else None,
        "stop_reason": result.stop_reason,
        "result_text": (result.result_text or "")[:600],
        "cwd": str(ws),
        "harness_dirs": harness_dirs,
        "tmp_new": sorted(set(after) - set(before)),
        "strace_lines": len(lines),
        "real_claude": real_claude,
        "python": sys.executable,
        # Every exec, clone and inotify watch, and every line naming the cwd,
        # a relative path, or /tmp; the test decides.
        "strace": [
            line for line in lines
            if str(ws) in line or "/tmp" in line
            or ('AT_FDCWD, "' in line and 'AT_FDCWD, "/' not in line)
            or "execve(" in line or "inotify_add_watch(" in line
            or "clone" in line or "fork" in line
        ],
        "pids": pids,
    }
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
