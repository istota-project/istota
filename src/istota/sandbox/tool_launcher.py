"""The command the ``claude`` CLI runs as its MCP server: the tool server, cleanly.

``python -I -m istota.sandbox.tool_launcher --report FIFO --env-file PATH -- ARGV...``,
written into the harness's ``--mcp-config`` by the daemon (#862). In order:

1. Clear the environment. The CLI passes its whole env to a stdio server
   whatever the config's ``env`` says: its credential, its messaging socket.
2. Report this pid down ``FIFO`` (``O_WRONLY | O_NONBLOCK | O_NOFOLLOW``, a
   FIFO by ``fstat``). No reader means the daemon is not listening, so nothing
   runs. The pid survives the exec, so the daemon learns the tool server's
   pid (bwrap's) and authorizes that, never the CLI's.
3. Read the env file (``O_NOFOLLOW``, a JSON object of strings) and unlink it.
   Reporting comes first, so a launcher the CLI respawns after the file is
   gone still reports, and the daemon refuses the second report.
4. ``execve(ARGV[0], ARGV, env)``. ``ARGV[0]`` must be absolute; nothing
   searches ``PATH``.

Any refusal exits 2 with one line on stderr naming the step, never a value.
The kernel's ``/proc/<pid>/environ`` block is replaced at the exec, so the
CLI's environment does not outlive this process.

stdlib-only, imports nothing from the package: it runs under ``python -I``
before any sandbox exists. ``tests/test_tool_launcher.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys

_EXIT_REFUSED = 2
_MAX_ENV_FILE_BYTES = 1 << 20


class _Refused(Exception):
    pass


def _parse_argv(argv: list[str]) -> tuple[str, str, list[str]]:
    if len(argv) < 6 or argv[0] != "--report" or argv[2] != "--env-file" or argv[4] != "--":
        raise _Refused("usage: --report FIFO --env-file PATH -- ARGV...")
    command = argv[5:]
    if not os.path.isabs(command[0]):
        raise _Refused("the command must be an absolute path")
    return argv[1], argv[3], command


def _report_pid(fifo: str) -> None:
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        raise _Refused(f"cannot open the pid report: {e.strerror}") from None
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise _Refused("the pid report is not a FIFO")
        line = b"%d\n" % os.getpid()
        if os.write(fd, line) != len(line):
            raise _Refused("short write to the pid report")
    except OSError as e:
        raise _Refused(f"cannot write the pid report: {e.strerror}") from None
    finally:
        os.close(fd)


def _read_env_file(path: str) -> dict[str, str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise _Refused(f"cannot open the env file: {e.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Refused("the env file is not a regular file")
        data = os.read(fd, _MAX_ENV_FILE_BYTES + 1)
    except OSError as e:
        raise _Refused(f"cannot read the env file: {e.strerror}") from None
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
    if len(data) > _MAX_ENV_FILE_BYTES:
        raise _Refused("the env file is too large")
    try:
        env = json.loads(data)
    except ValueError:
        raise _Refused("the env file is not JSON") from None
    if not isinstance(env, dict):
        raise _Refused("the env file is not a JSON object")
    for name, value in env.items():
        if not name or "=" in name or "\0" in name:
            raise _Refused("the env file holds an invalid name")
        if not isinstance(value, str) or "\0" in value:
            raise _Refused("the env file holds a value that is not a string")
    return env


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    os.environ.clear()
    try:
        fifo, env_path, command = _parse_argv(argv)
        _report_pid(fifo)
        env = _read_env_file(env_path)
    except _Refused as e:
        print(f"istota tool launcher: {e}", file=sys.stderr)
        return _EXIT_REFUSED
    try:
        os.execve(command[0], command, env)
    except OSError as e:
        print(f"istota tool launcher: cannot exec the command: {e.strerror}", file=sys.stderr)
    return _EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
