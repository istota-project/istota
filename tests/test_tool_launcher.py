"""The launcher the ``claude`` CLI spawns in front of the tool server (#862).

``python -I -m istota.sandbox.tool_launcher --report FIFO --env-file PATH -- ARGV``
clears the CLI's environment, reports its pid down the FIFO, loads the env
file (unlinking it) and execs ``ARGV`` with it. Every case runs the real
launcher as a subprocess against a real FIFO, and reads the report with
``peer_process.split_pid_lines``, the parser the daemon's reader shares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota.sandbox import peer_process

SENTINEL_NAME = "ISTOTA_LAUNCHER_TEST_SECRET"
SENTINEL_VALUE = "launcher-env-canary-value"
ENV_FILE_ENV = {"PATH": "/usr/bin:/bin", "ISTOTA_LAUNCHER_PASSED": "yes"}


class _Fifo:
    """The daemon's end: a FIFO whose read end is open before the launcher runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path, 0o600)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def pids(self) -> list[int]:
        buffered = b""
        while True:
            try:
                chunk = os.read(self.fd, 64)
            except BlockingIOError:
                break
            if not chunk:
                break
            buffered += chunk
        pids, rest = peer_process.split_pid_lines(buffered)
        assert rest == b""
        return pids

    def close(self) -> None:
        os.close(self.fd)


@pytest.fixture
def fifo(tmp_path):
    f = _Fifo(tmp_path / "pid.fifo")
    yield f
    f.close()


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "tool-env.json"
    path.write_text(json.dumps(ENV_FILE_ENV))
    path.chmod(0o600)
    return path


def _launch(report: Path, env_file: Path, argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    parent_env = {**os.environ, SENTINEL_NAME: SENTINEL_VALUE}
    return subprocess.run(
        [sys.executable, "-I", "-m", "istota.sandbox.tool_launcher",
         "--report", str(report), "--env-file", str(env_file), "--", *argv],
        capture_output=True, env=parent_env, cwd=cwd, timeout=60,
    )


def _environ_lines(stdout: bytes) -> dict[str, str]:
    env = {}
    for line in stdout.decode().splitlines():
        name, _, value = line.partition("=")
        env[name] = value
    return env


class TestTheExec:
    def test_the_child_gets_the_env_file_and_nothing_of_the_parent(self, tmp_path, fifo, env_file):
        proc = _launch(fifo.path, env_file, ["/usr/bin/env"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert SENTINEL_VALUE.encode() not in proc.stdout
        assert SENTINEL_NAME.encode() not in proc.stdout
        assert _environ_lines(proc.stdout) == ENV_FILE_ENV

    def test_the_reported_pid_is_the_execd_childs(self, tmp_path, fifo, env_file):
        proc = _launch(fifo.path, env_file, ["/bin/sh", "-c", "echo $$"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert fifo.pids() == [int(proc.stdout)]

    def test_the_env_file_is_unlinked(self, tmp_path, fifo, env_file):
        proc = _launch(fifo.path, env_file, ["/usr/bin/true"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert not env_file.exists()

    def test_the_child_exit_status_is_the_launchers(self, tmp_path, fifo, env_file):
        proc = _launch(fifo.path, env_file, ["/bin/sh", "-c", "exit 7"], tmp_path)
        assert proc.returncode == 7


class TestNothingRuns:
    """Every refusal exits 2 and the command never runs: no marker file."""

    @staticmethod
    def _touch(tmp_path: Path) -> tuple[list[str], Path]:
        marker = tmp_path / "ran"
        return ["/usr/bin/touch", str(marker)], marker

    def test_no_reader_on_the_fifo(self, tmp_path, env_file):
        report = tmp_path / "pid.fifo"
        os.mkfifo(report, 0o600)
        argv, marker = self._touch(tmp_path)
        proc = _launch(report, env_file, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()

    def test_no_fifo_at_all(self, tmp_path, env_file):
        argv, marker = self._touch(tmp_path)
        proc = _launch(tmp_path / "missing.fifo", env_file, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()

    def test_a_regular_file_is_not_a_fifo(self, tmp_path, env_file):
        report = tmp_path / "pid.fifo"
        report.write_bytes(b"")
        argv, marker = self._touch(tmp_path)
        proc = _launch(report, env_file, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()
        assert report.read_bytes() == b""

    def test_a_symlinked_fifo_is_refused(self, tmp_path, fifo, env_file):
        link = tmp_path / "link.fifo"
        link.symlink_to(fifo.path)
        argv, marker = self._touch(tmp_path)
        proc = _launch(link, env_file, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()
        assert fifo.pids() == []

    def test_a_missing_env_file_still_reports(self, tmp_path, fifo):
        # The report comes first, so a respawned launcher (its env file
        # already unlinked) is still seen, and refused, by the daemon.
        argv, marker = self._touch(tmp_path)
        proc = _launch(fifo.path, tmp_path / "gone.json", argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()
        assert len(fifo.pids()) == 1

    @pytest.mark.parametrize("content", [
        "not json",
        "[]",
        '{"PATH": 1}',
        '{"PATH": null}',
        '{"A": "x\\u0000y"}',
        '{"A=B": "x"}',
        '{"": "x"}',
    ])
    def test_a_malformed_env_file(self, tmp_path, fifo, env_file, content):
        env_file.write_text(content)
        argv, marker = self._touch(tmp_path)
        proc = _launch(fifo.path, env_file, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()
        assert not env_file.exists()

    def test_a_symlinked_env_file_is_refused(self, tmp_path, fifo, env_file):
        link = tmp_path / "link.json"
        link.symlink_to(env_file)
        argv, marker = self._touch(tmp_path)
        proc = _launch(fifo.path, link, argv, tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()

    def test_a_relative_argv0_is_not_searched(self, tmp_path, fifo, env_file):
        marker = tmp_path / "ran"
        proc = _launch(fifo.path, env_file, ["touch", str(marker)], tmp_path)
        assert proc.returncode == 2
        assert not marker.exists()
        assert fifo.pids() == []

    @pytest.mark.parametrize("argv", [
        ["--env-file", "x", "--", "/usr/bin/true"],
        ["--report", "x", "--", "/usr/bin/true"],
        ["--report", "x", "--env-file", "y", "/usr/bin/true"],
        ["--report", "x", "--env-file", "y", "--"],
        [],
    ])
    def test_a_malformed_command_line(self, tmp_path, argv):
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "istota.sandbox.tool_launcher", *argv],
            capture_output=True, cwd=tmp_path, timeout=60,
        )
        assert proc.returncode == 2


def test_the_environment_is_cleared_before_the_report(tmp_path, fifo, env_file):
    # The exec passes the env file explicitly, so the child's environ alone
    # cannot show the clear; this records what the launcher itself held.
    probe = (
        "import json, os, sys\n"
        "from istota.sandbox import tool_launcher as t\n"
        "seen = {}\n"
        "real_report = t._report_pid\n"
        "def report(fifo):\n"
        "    seen['at_report'] = dict(os.environ)\n"
        "    real_report(fifo)\n"
        "def execve(path, argv, env):\n"
        "    seen['at_exec'] = dict(os.environ)\n"
        "    seen['env'] = env\n"
        "    print(json.dumps(seen)); sys.exit(0)\n"
        "t._report_pid = report\n"
        "os.execve = execve\n"
        "sys.exit(t.main(sys.argv[1:]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe,
         "--report", str(fifo.path), "--env-file", str(env_file), "--", "/usr/bin/true"],
        capture_output=True, env={**os.environ, SENTINEL_NAME: SENTINEL_VALUE},
        cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert seen == {"at_report": {}, "at_exec": {}, "env": ENV_FILE_ENV}


def test_nothing_secret_reaches_stderr(tmp_path, fifo, env_file):
    env_file.write_text(json.dumps({"PATH": 1, "SECRETISH": "env-file-value"}))
    proc = _launch(fifo.path, env_file, ["/usr/bin/true"], tmp_path)
    assert proc.returncode == 2
    assert b"env-file-value" not in proc.stderr
    assert SENTINEL_VALUE.encode() not in proc.stderr
    assert proc.stdout == b""


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="needs /proc")
def test_the_kernel_environ_block_holds_no_sentinel(tmp_path, fifo, env_file):
    out = tmp_path / "environ"
    script = f"cat /proc/$$/environ > {out}"
    proc = _launch(fifo.path, env_file, ["/bin/sh", "-c", script], tmp_path)
    assert proc.returncode == 0, proc.stderr
    seen = out.read_bytes()
    assert SENTINEL_VALUE.encode() not in seen
    assert b"ISTOTA_LAUNCHER_PASSED=yes" in seen


def test_the_launcher_imports_nothing_outside_the_stdlib():
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import istota.sandbox.tool_launcher\n"
        "new = set(sys.modules) - before\n"
        "mods = sorted(m for m in new if m.split('.')[0] not in sys.stdlib_module_names)\n"
        "print(','.join(mods))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    loaded = {m for m in tail.split(",") if m}
    assert loaded <= {"istota", "istota.sandbox", "istota.sandbox.tool_launcher"}, loaded
