"""Tests for scripts/qt, the incremental test runner.

The failure this file exists for: testmon aborts a session with an
INTERNALERROR rather than a test failure, so every test passes and the run
still means nothing. `qt` catches that and falls back -- and the fallback used
to be another `--testmon` run, which reran the same abort. A fresh worktree hit
it twice on its first `qt`, because the run that builds `.testmondata` is a
full `--testmon` run too.

`qt` runs from the parent of its own directory, so these tests give it a
throwaway one: a `scripts/` holding a copy of `qt` and a `qtest` stub that just
execs its arguments (the real one takes a machine-wide lock), and a fake `uv`
on PATH that records every argv it is handed.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QT = REPO_ROOT / "scripts" / "qt"

QTEST_STUB = """\
#!/usr/bin/env bash
exec "$@"
"""

#: Records its argv one line per invocation, then answers from a canned script:
#: the Nth line of $FAKE_UV_PLAN is "<exit code> <text to print>".
FAKE_UV = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
n=$(wc -l < "$FAKE_UV_LOG" | tr -d ' ')
line=$(sed -n "${n}p" "$FAKE_UV_PLAN")
[ -z "$line" ] && line=$(tail -n 1 "$FAKE_UV_PLAN")
code=${line%% *}
echo "${line#* }"
exit "$code"
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def qt_tree(tmp_path):
    """A throwaway checkout holding `scripts/qt`, a `qtest` stub and a fake `uv`."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(QT, scripts / "qt")
    _executable(scripts / "qtest", QTEST_STUB)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "uv", FAKE_UV)
    return tmp_path


def run_qt(tree: Path, plan: list[str], *args):
    """Run qt with `plan` as the fake `uv`'s canned answers."""
    log = tree / "uv.log"
    log.write_text("")
    (tree / "uv.plan").write_text("".join(line + "\n" for line in plan))
    env = {
        **os.environ,
        "PATH": f"{tree / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_PLAN": str(tree / "uv.plan"),
    }
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "qt"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(tree),
    )
    return proc, [line for line in log.read_text().splitlines() if line]


class TestTheInternalerrorFallback:
    def test_it_does_not_rerun_testmon(self, qt_tree):
        """The regression. Falling back to another `--testmon` run reran the abort."""
        (qt_tree / ".testmondata").write_text("")

        proc, calls = run_qt(qt_tree, ["3 INTERNALERROR> boom", "0 1 passed"])

        assert len(calls) == 2, proc.stderr
        assert "--testmon" in calls[0]
        assert "--testmon" not in calls[1]
        assert proc.returncode == 0

    def test_it_moves_the_data_file_aside_so_the_next_run_rebuilds(self, qt_tree):
        (qt_tree / ".testmondata").write_text("half-written")

        run_qt(qt_tree, ["3 INTERNALERROR> boom", "0 1 passed"])

        assert not (qt_tree / ".testmondata").exists()
        assert (qt_tree / ".testmondata.aborted").read_text() == "half-written"

    def test_the_fallback_failing_is_not_reported_as_success(self, qt_tree):
        (qt_tree / ".testmondata").write_text("")

        proc, _ = run_qt(qt_tree, ["3 INTERNALERROR> boom", "1 1 failed"])

        assert proc.returncode == 1


class TestTheOrdinaryPaths:
    def test_no_data_file_builds_one_with_testmon(self, qt_tree):
        proc, calls = run_qt(qt_tree, ["0 1 passed"])

        assert len(calls) == 1, proc.stderr
        assert "--testmon" in calls[0]

    def test_the_incremental_run_excludes_no_test_file(self, qt_tree):
        """`tests/test_devbox_exec_server.py` was excluded here until the shim in
        `tests/support/testmon_compat.py` made its import survivable. An
        exclusion left behind would hide that file from the edit loop for good.
        """
        (qt_tree / ".testmondata").write_text("")

        _, calls = run_qt(qt_tree, ["0 1 passed"])

        assert "--ignore" not in calls[0]

    def test_pytests_no_tests_ran_is_not_a_failure(self, qt_tree):
        (qt_tree / ".testmondata").write_text("")

        proc, _ = run_qt(qt_tree, ["5 no tests ran"])

        assert proc.returncode == 0
        assert "nothing affected" in proc.stderr
