"""Tests for ``istota.task_cgroup``.

Every function in that module takes its roots as parameters, the way
``host_pressure`` does, so these tests build a fake cgroup tree under
``tmp_path`` and point the module at it. Nothing here touches
``/sys/fs/cgroup`` or ``/proc`` — the suite runs on macOS dev machines where
neither exists.

A fake tree does differ from a real cgroup2fs in one way that matters, and the
module leans on it deliberately: on the real thing the kernel makes the
interface files and a writer cannot create them, so a successful ``memory.max``
write is proof the controller is enabled. Under ``tmp_path`` the write creates
the file instead. That is why the fail-open cases here are produced by taking
*permissions* away rather than by deleting a file — a read-only directory
raises the same ``OSError`` from the same call the missing controller would.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from istota import task_cgroup


@pytest.fixture(autouse=True)
def _fresh_log_state():
    """One-shot warnings are module state; reset it around every test.

    The suite runs under ``-n auto`` and these tests share a worker process, so
    without this a test asserting "logs once" and a test asserting "logs at
    all" would depend on which ran first.
    """
    task_cgroup._reset_log_state()
    yield
    task_cgroup._reset_log_state()


@pytest.fixture
def cgroup_root(tmp_path: Path) -> Path:
    """A delegated unit cgroup with the daemon's own `supervisor/` leaf in it."""
    root = tmp_path / "sys" / "fs" / "cgroup" / "system.slice" / "istota-scheduler.service"
    (root / "supervisor").mkdir(parents=True)
    (root / "cgroup.subtree_control").write_text("")
    return root


DEFAULT_LIMITS = task_cgroup.CgroupLimits(
    memory_max_mb=2048, pids_max=512, cpu_max_percent=200
)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_writes_all_three_limits_with_the_configured_values(self, cgroup_root):
        path = task_cgroup.create(41, DEFAULT_LIMITS, root=cgroup_root)

        assert path == cgroup_root / "task-41"
        assert path.is_dir()
        # 2048 MB in bytes — the kernel takes bytes, and an implementation that
        # wrote megabytes would cap a task at 2 kB and fail every task on the host.
        assert (path / "memory.max").read_text().strip() == str(2048 * 1024 * 1024)
        assert (path / "pids.max").read_text().strip() == "512"
        # cgroup v2 spells a CPU limit "<quota_us> <period_us>"; 200% of one
        # core over the kernel's default 100 ms period is 200,000 us.
        assert (path / "cpu.max").read_text().strip() == "200000 100000"

    def test_cpu_max_is_omitted_entirely_when_the_percent_is_zero(self, cgroup_root):
        limits = task_cgroup.CgroupLimits(
            memory_max_mb=512, pids_max=64, cpu_max_percent=0
        )

        path = task_cgroup.create(7, limits, root=cgroup_root)

        # Not written as `max`: 0 means "leave CPU alone", and an operator
        # reading the tree should see the knob they set rather than a value.
        assert not (path / "cpu.max").exists()
        assert (path / "memory.max").read_text().strip() == str(512 * 1024 * 1024)
        assert (path / "pids.max").read_text().strip() == "64"

    def test_zero_memory_writes_max_rather_than_a_zero_byte_limit(self, cgroup_root):
        limits = task_cgroup.CgroupLimits(memory_max_mb=0, pids_max=0, cpu_max_percent=0)

        path = task_cgroup.create(8, limits, root=cgroup_root)

        # `memory.max = 0` would OOM-kill the task's first allocation. The
        # opt-out spelling is the kernel's own.
        assert (path / "memory.max").read_text().strip() == "max"
        assert (path / "pids.max").read_text().strip() == "max"

    def test_returns_none_and_logs_once_when_the_root_is_not_writable(
        self, tmp_path, caplog
    ):
        # The fail-open assertion, and the one that matters most: a deployment
        # that has not run the updated unit file must keep working.
        root = tmp_path / "locked"
        root.mkdir()
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
                first = task_cgroup.create(1, DEFAULT_LIMITS, root=root)
                second = task_cgroup.create(2, DEFAULT_LIMITS, root=root)
        finally:
            os.chmod(root, stat.S_IRWXU)

        assert first is None
        assert second is None
        # Once per process, not once per task. At the default dispatch cadence
        # a per-task warning is a line every few minutes forever.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "uncontained" in warnings[0].getMessage()

    def test_returns_none_and_removes_the_directory_when_memory_max_is_unwritable(
        self, cgroup_root, caplog, monkeypatch
    ):
        # The real shape of "no memory controller delegated": the directory is
        # created but the kernel never puts `memory.max` in it, so the write
        # fails. An empty cgroup left behind would read as containment in
        # `systemd-cgls` while containing nothing.
        real_write = Path.write_text

        def refuse_memory_max(self, *args, **kwargs):
            if self.name == "memory.max":
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", refuse_memory_max)

        with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
            path = task_cgroup.create(9, DEFAULT_LIMITS, root=cgroup_root)

        assert path is None
        assert not (cgroup_root / "task-9").exists()
        assert "memory.max" in caplog.text

    def test_keeps_the_cgroup_when_only_pids_or_cpu_are_unavailable(
        self, cgroup_root, monkeypatch
    ):
        # Memory-only containment is most of the value, so a missing pids or cpu
        # controller must not throw the memory limit away with it.
        real_write = Path.write_text

        def refuse_pids_and_cpu(self, *args, **kwargs):
            if self.name in ("pids.max", "cpu.max"):
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", refuse_pids_and_cpu)

        path = task_cgroup.create(10, DEFAULT_LIMITS, root=cgroup_root)

        assert path is not None
        assert (path / "memory.max").read_text().strip() == str(2048 * 1024 * 1024)

    def test_enables_controllers_on_the_root_one_at_a_time(self, cgroup_root):
        task_cgroup.create(11, DEFAULT_LIMITS, root=cgroup_root)

        # Written separately rather than as one "+memory +pids +cpu" line: that
        # write is all-or-nothing, so a host missing `cpu` would lose `memory`
        # with it. Under tmp_path each write replaces the last, so the file
        # holds the final one and the point is that all three were attempted.
        assert (cgroup_root / "cgroup.subtree_control").read_text().strip() == "+cpu"

    def test_reuses_an_existing_directory_rather_than_failing(self, cgroup_root):
        # A task id can recur across a daemon restart that left its cgroup behind.
        (cgroup_root / "task-12").mkdir()
        (cgroup_root / "task-12" / "memory.max").write_text("999\n")

        path = task_cgroup.create(12, DEFAULT_LIMITS, root=cgroup_root)

        assert path is not None
        assert (path / "memory.max").read_text().strip() == str(2048 * 1024 * 1024)


# ---------------------------------------------------------------------------
# place
# ---------------------------------------------------------------------------


class TestPlace:
    def test_writes_the_pid_to_cgroup_procs(self, cgroup_root):
        path = task_cgroup.create(20, DEFAULT_LIMITS, root=cgroup_root)

        assert task_cgroup.place(4242, path) is True
        assert (path / "cgroup.procs").read_text().strip() == "4242"

    def test_a_pid_that_already_exited_is_not_an_error(self, cgroup_root, caplog):
        # ESRCH is the kernel's answer for "that process is gone", which is a
        # race the caller cannot avoid: the pid is handed over after spawn.
        path = task_cgroup.create(21, DEFAULT_LIMITS, root=cgroup_root)
        real_write = Path.write_text

        def gone(self, *args, **kwargs):
            if self.name == "cgroup.procs":
                raise ProcessLookupError(3, "No such process")
            return real_write(self, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "write_text", gone)
            with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
                assert task_cgroup.place(4242, path) is False

        # Not worth a warning: nothing is running, so nothing is uncontained.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_missing_cgroup_is_reported_once_and_never_raises(self, tmp_path, caplog):
        missing = tmp_path / "task-99"

        with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
            assert task_cgroup.place(4242, missing) is False
            assert task_cgroup.place(4243, missing) is False

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "uncontained" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# destroy / sweep_stale
# ---------------------------------------------------------------------------


class TestDestroy:
    def test_removes_the_directory(self, cgroup_root):
        path = task_cgroup.create(30, DEFAULT_LIMITS, root=cgroup_root)
        # A real cgroup directory holds only kernel files, which vanish with it.
        for f in path.iterdir():
            f.unlink()

        task_cgroup.destroy(path)

        assert not path.exists()

    def test_on_an_already_removed_directory_is_a_no_op(self, tmp_path):
        task_cgroup.destroy(tmp_path / "task-31")  # must not raise

    def test_a_cgroup_that_still_holds_processes_is_left_alone(self, cgroup_root):
        # rmdir on a non-empty directory is the fixture's stand-in for the
        # kernel's EBUSY. Removing a cgroup does not kill anything, so there is
        # nothing useful to do but leave it for the next startup sweep.
        path = task_cgroup.create(32, DEFAULT_LIMITS, root=cgroup_root)

        task_cgroup.destroy(path)

        assert path.exists()


class TestSweepStale:
    def test_removes_task_directories_and_leaves_everything_else(self, cgroup_root):
        for name in ("task-1", "task-2"):
            (cgroup_root / name).mkdir()
        (cgroup_root / "other").mkdir()

        removed = task_cgroup.sweep_stale(cgroup_root)

        assert removed == 2
        assert not (cgroup_root / "task-1").exists()
        assert not (cgroup_root / "task-2").exists()
        # `supervisor/` is where the daemon doing the sweeping is sitting, and
        # it is a sibling of the task cgroups. The prefix match is what keeps
        # this from removing it.
        assert (cgroup_root / "supervisor").is_dir()
        assert (cgroup_root / "other").is_dir()

    def test_a_task_cgroup_that_is_still_busy_is_not_counted_as_removed(
        self, cgroup_root
    ):
        (cgroup_root / "task-1").mkdir()
        busy = cgroup_root / "task-2"
        busy.mkdir()
        (busy / "cgroup.procs").write_text("1234\n")

        removed = task_cgroup.sweep_stale(cgroup_root)

        assert removed == 1
        assert busy.is_dir()

    def test_an_unreadable_root_returns_zero_rather_than_raising(self, tmp_path):
        assert task_cgroup.sweep_stale(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# resolve_root
# ---------------------------------------------------------------------------


def _proc_tree(tmp_path: Path, cgroup_line: str) -> Path:
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text(cgroup_line)
    return tmp_path / "proc"


class TestResolveRoot:
    def test_truncates_the_delegate_subgroup_leaf_to_the_unit_cgroup(self, tmp_path):
        # This is the whole trick. `/proc/self/cgroup` reports the `supervisor/`
        # leaf `DelegateSubgroup=` put the daemon in, and that leaf holds
        # processes — so a `task-N/` made inside it would get no controller
        # files at all. The unit cgroup above it is the delegated one.
        proc = _proc_tree(tmp_path, "0::/system.slice/istota-scheduler.service/supervisor\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        (cg / "system.slice" / "istota-scheduler.service" / "supervisor").mkdir(parents=True)

        root = task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg)

        assert root == cg / "system.slice" / "istota-scheduler.service"

    def test_a_daemon_sitting_directly_in_the_unit_cgroup_resolves_to_it(self, tmp_path):
        # Older systemd ignores `DelegateSubgroup=`. Resolution still succeeds;
        # what fails is the `memory.max` write, because the unit cgroup holds
        # the daemon and so cannot enable controllers. That degradation is
        # `create`'s to report, not this function's to pre-empt.
        proc = _proc_tree(tmp_path, "0::/system.slice/istota-scheduler.service\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        (cg / "system.slice" / "istota-scheduler.service").mkdir(parents=True)

        root = task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg)

        assert root == cg / "system.slice" / "istota-scheduler.service"

    def test_returns_none_when_no_systemd_unit_is_in_the_path(self, tmp_path):
        # A dev machine, or a container whose cgroup names no unit. There is no
        # subtree this module can be confident it owns, and mkdir-ing into one
        # it does not own is worse than leaving the task uncontained.
        proc = _proc_tree(tmp_path, "0::/\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        cg.mkdir(parents=True)

        assert task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg) is None

    def test_ignores_a_cgroup_v1_line(self, tmp_path):
        # A v1 line names a controller-specific hierarchy with no
        # `cgroup.subtree_control` in it, so matching loosely would build a path
        # that either does not exist or means something else entirely.
        proc = _proc_tree(tmp_path, "11:memory:/system.slice/istota-scheduler.service\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        (cg / "system.slice" / "istota-scheduler.service").mkdir(parents=True)

        assert task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg) is None

    def test_a_dotdot_in_the_cgroup_line_cannot_escape_the_root(self, tmp_path):
        # Built so the *unfiltered* join would land on a directory that really
        # exists outside `cgroup_root`. Asserting `is None` against a target
        # that does not exist would pass against an implementation with no
        # traversal guard at all.
        proc = _proc_tree(tmp_path, "0::/../../escaped.service/supervisor\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        cg.mkdir(parents=True)
        escaped = tmp_path / "sys" / "escaped.service"
        (escaped / "supervisor").mkdir(parents=True)
        assert cg.joinpath("..", "..", "escaped.service").resolve() == escaped

        root = task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg)

        assert root != escaped
        # With `..` dropped the remainder names nothing under the root, so
        # there is no delegated subtree to report.
        assert root is None

    def test_returns_none_when_proc_is_absent(self, tmp_path):
        assert (
            task_cgroup.resolve_root(
                proc_root=tmp_path / "nothing", cgroup_root=tmp_path / "nothing"
            )
            is None
        )

    def test_returns_none_when_the_resolved_directory_does_not_exist(self, tmp_path):
        proc = _proc_tree(tmp_path, "0::/system.slice/istota-scheduler.service/supervisor\n")
        cg = tmp_path / "sys" / "fs" / "cgroup"
        cg.mkdir(parents=True)

        assert task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg) is None
