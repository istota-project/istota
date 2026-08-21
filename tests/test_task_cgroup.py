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

import errno
import logging
import os
import stat
import sys
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


# The interface files the kernel puts in a cgroup directory. They are not
# ordinary dirents: nothing can unlink them, and they cease to exist with the
# cgroup, so `rmdir` on a cgroup holding only these succeeds. Under `tmp_path`
# they are real files and `rmdir` gets ENOTEMPTY — the one way this fixture
# differs from the thing it stands for, and the difference is load-bearing
# because `destroy` distinguishes "empty" from "still holds processes".
_INTERFACE_FILES = frozenset({
    "cgroup.procs", "cgroup.kill", "cgroup.subtree_control",
    "memory.max", "memory.events", "pids.max", "cpu.max",
})

_REAL_RMDIR = Path.rmdir


def _kernelish_rmdir(self: Path) -> None:
    """`rmdir` with cgroupfs semantics: interface files go with the directory."""
    if self.is_dir():
        for child in self.iterdir():
            if child.name in _INTERFACE_FILES:
                child.unlink()
    _REAL_RMDIR(self)


@pytest.fixture
def cgroupfs(monkeypatch):
    """Make `rmdir` behave the way it does on a real cgroup2fs.

    Kept in one place rather than unlinked ad hoc per test: three tests worked
    around this individually in the first cut, and two of them ended up
    asserting the opposite of production behaviour because an ordinary finished
    cgroup and one still holding processes both raise from `rmdir` here.
    """
    monkeypatch.setattr(Path, "rmdir", _kernelish_rmdir)


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

    def test_enables_controllers_on_the_root_one_at_a_time(
        self, cgroup_root, monkeypatch
    ):
        # Asserting on the file's *contents* cannot test this. Under tmp_path
        # each write truncates, so the file holds only the last one and an
        # implementation that enabled `cpu` alone reads identically to one that
        # enabled all three — verified against a mutant. On a real cgroupfs the
        # file is additive, so the fixture and the kernel disagree in opposite
        # directions. Record the writes instead.
        writes = []
        real_write = Path.write_text

        def record(self, data, *args, **kwargs):
            if self.name == "cgroup.subtree_control":
                writes.append(data.strip())
            return real_write(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", record)

        task_cgroup.create(11, DEFAULT_LIMITS, root=cgroup_root)

        # Separately, not as one "+memory +pids +cpu" line: that write is
        # all-or-nothing, so a host missing `cpu` would lose `memory` with it.
        assert writes == ["+memory", "+pids", "+cpu"]

    def test_an_attempt_gets_its_own_directory(self, cgroup_root):
        # A retry reuses the task row, so the id alone is not unique over time.
        # Attempt 1's directory survives precisely when its tree escaped the
        # kill — and sharing a budget with that runaway is how attempt 2 gets
        # OOM-killed on the spot for a fault that is not its own.
        first = task_cgroup.create(12, DEFAULT_LIMITS, attempt=1, root=cgroup_root)
        second = task_cgroup.create(12, DEFAULT_LIMITS, attempt=2, root=cgroup_root)

        assert first == cgroup_root / "task-12-1"
        assert second == cgroup_root / "task-12-2"
        assert first != second
        # Both still match the sweep's prefix, so neither can outlive a restart.
        assert first.name.startswith("task-") and second.name.startswith("task-")

    def test_reuses_an_existing_directory_rather_than_failing(self, cgroup_root):
        # Same attempt twice (a daemon restart mid-attempt) must not be an error.
        (cgroup_root / "task-13-1").mkdir()
        (cgroup_root / "task-13-1" / "memory.max").write_text("999\n")

        path = task_cgroup.create(13, DEFAULT_LIMITS, attempt=1, root=cgroup_root)

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

    def test_a_different_failure_is_not_muted_by_an_earlier_one(
        self, tmp_path, caplog, monkeypatch
    ):
        # The suppression key is per-errno, not a constant. With one shared key
        # the first uncontained task would be the only one ever reported and
        # every later one would be silent — which is the "containment engaged
        # and containment never engaged must not look alike" rule this module
        # states for itself, broken by its own logging.
        path = tmp_path / "task-98"
        path.mkdir()
        real_write = Path.write_text
        errno_to_raise = [errno.EACCES]

        def fail(self, *args, **kwargs):
            if self.name == "cgroup.procs":
                raise OSError(errno_to_raise[0], "denied")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail)

        with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
            task_cgroup.place(1, path)
            task_cgroup.place(2, path)      # same errno — muted
            errno_to_raise[0] = errno.ENOENT
            task_cgroup.place(3, path)      # different cause — must be heard

        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


class TestProbe:
    """The startup report has to be a measurement, not a restatement.

    `resolve_root` answers on every systemd host whether or not `Delegate=` was
    ever applied, so a report built on it alone printed the affirmative line on
    exactly the deployments where every `create` would go on to fail. That is
    the failure the spec's A6 notes name — "containment would never engage and
    nothing would report it" — reintroduced by the line written to prevent it.
    """

    def test_returns_none_when_containment_will_work(self, cgroup_root, cgroupfs):
        assert task_cgroup.probe(cgroup_root) is None
        # And leaves nothing behind.
        assert not (cgroup_root / "task-probe").exists()

    def test_names_the_reason_when_the_memory_controller_is_absent(
        self, cgroup_root, cgroupfs, monkeypatch
    ):
        real_write = Path.write_text

        def no_memory(self, *args, **kwargs):
            if self.name == "memory.max":
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", no_memory)

        reason = task_cgroup.probe(cgroup_root)

        assert reason is not None
        assert "memory" in reason
        assert not (cgroup_root / "task-probe").exists()

    def test_names_the_reason_when_the_root_is_not_writable(self, tmp_path):
        root = tmp_path / "locked"
        root.mkdir()
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        try:
            reason = task_cgroup.probe(root)
        finally:
            os.chmod(root, stat.S_IRWXU)

        assert reason is not None
        assert "cannot create" in reason


class TestReadEvents:
    def test_parses_the_counters(self, cgroup_root):
        path = cgroup_root / "task-40"
        path.mkdir()
        (path / "memory.events").write_text("low 0\nhigh 12\nmax 3\noom 1\noom_kill 2\n")

        assert task_cgroup.read_events(path)["oom_kill"] == 2

    def test_an_absent_file_is_an_empty_mapping_not_a_raise(self, tmp_path):
        # Read on the task's exit path, where an exception would replace the
        # task's real result with this one's.
        assert task_cgroup.read_events(tmp_path / "gone") == {}

    def test_a_malformed_line_is_skipped(self, cgroup_root):
        path = cgroup_root / "task-41"
        path.mkdir()
        (path / "memory.events").write_text("oom_kill notanumber\nhigh 5\n")

        assert task_cgroup.read_events(path) == {"high": 5}


# ---------------------------------------------------------------------------
# destroy / sweep_stale
# ---------------------------------------------------------------------------


class TestDestroy:
    def test_removes_an_ordinary_finished_cgroup(self, cgroup_root, cgroupfs):
        # The production shape: limits written, no processes left. This is the
        # case the first cut of these tests had asserting the *opposite*,
        # because under a plain tmp_path it is indistinguishable from a busy one.
        path = task_cgroup.create(30, DEFAULT_LIMITS, root=cgroup_root)

        assert task_cgroup.destroy(path) is True
        assert not path.exists()

    def test_on_an_already_removed_directory_is_a_no_op(self, tmp_path):
        assert task_cgroup.destroy(tmp_path / "task-31") is True  # must not raise

    def test_a_cgroup_that_still_holds_processes_is_killed_then_removed(
        self, cgroup_root, monkeypatch
    ):
        # EBUSY is stubbed rather than stood in for by ENOTEMPTY. The earlier
        # version of this test used a directory that still held its limit files,
        # which is ENOTEMPTY under tmp_path and *removable* on a real cgroupfs —
        # so it asserted the opposite of production behaviour for an input that
        # is simply an ordinary finished cgroup.
        path = cgroup_root / "task-32"
        path.mkdir()
        attempts = []

        def busy_once(self):
            attempts.append(self)
            if len(attempts) == 1:
                raise OSError(errno.EBUSY, "Device or resource busy")
            return _kernelish_rmdir(self)

        monkeypatch.setattr(Path, "rmdir", busy_once)
        kills = []
        real_write = Path.write_text

        def record_kill(self, data, *args, **kwargs):
            if self.name == "cgroup.kill":
                kills.append(data.strip())
            return real_write(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", record_kill)

        assert task_cgroup.destroy(path) is True
        # cgroup.kill (Linux 5.14+) kills every member including descendants
        # that escaped their process group — the guarantee killpg cannot make,
        # and the ISSUE-257 shape this whole module exists for. Recorded at
        # write time: by the time destroy returns, the directory is gone.
        assert kills == ["1"]
        assert len(attempts) == 2
        assert not path.exists()

    def test_reports_failure_when_the_kill_itself_cannot_be_written(
        self, cgroup_root, monkeypatch
    ):
        # A cgroup that cannot be emptied is a live runaway, which is the one
        # thing this module must not report as fine.
        path = cgroup_root / "task-33"
        path.mkdir()
        monkeypatch.setattr(
            Path, "rmdir", lambda self: (_ for _ in ()).throw(
                OSError(errno.EBUSY, "Device or resource busy")
            )
        )
        real_write = Path.write_text

        def no_kill(self, *args, **kwargs):
            if self.name == "cgroup.kill":
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", no_kill)

        assert task_cgroup.destroy(path) is False


class TestSweepStale:
    def test_removes_task_directories_and_leaves_everything_else(self, cgroup_root):
        for name in ("task-1", "task-2"):
            (cgroup_root / name).mkdir()
        (cgroup_root / "other").mkdir()

        removed, surviving = task_cgroup.sweep_stale(cgroup_root)

        assert (removed, surviving) == (2, 0)
        assert not (cgroup_root / "task-1").exists()
        assert not (cgroup_root / "task-2").exists()
        # `supervisor/` is where the daemon doing the sweeping is sitting, and
        # it is a sibling of the task cgroups. The prefix match is what keeps
        # this from removing it.
        assert (cgroup_root / "supervisor").is_dir()
        assert (cgroup_root / "other").is_dir()

    def test_a_cgroup_the_sweep_cannot_empty_is_counted_and_named(
        self, cgroup_root, monkeypatch, caplog
    ):
        # The interesting case: the daemon was killed and the tree that killed
        # it was not. Counting only removals reports that identically to a
        # clean start, which is the one reading an operator must not get.
        (cgroup_root / "task-1").mkdir()
        busy = cgroup_root / "task-2"
        busy.mkdir()
        real_rmdir = Path.rmdir

        def busy_forever(self):
            if self.name == "task-2":
                raise OSError(errno.EBUSY, "Device or resource busy")
            return real_rmdir(self)

        monkeypatch.setattr(Path, "rmdir", busy_forever)
        real_write = Path.write_text
        monkeypatch.setattr(
            Path, "write_text",
            lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "nope"))
            if self.name == "cgroup.kill" else real_write(self, *a, **k),
        )

        with caplog.at_level(logging.WARNING, logger="istota.task_cgroup"):
            removed, surviving = task_cgroup.sweep_stale(cgroup_root)

        assert (removed, surviving) == (1, 1)
        assert busy.is_dir()
        assert "task-2" in caplog.text

    def test_an_unreadable_root_returns_zero_rather_than_raising(self, tmp_path):
        assert task_cgroup.sweep_stale(tmp_path / "nope") == (0, 0)


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

    def test_takes_the_last_unit_component_not_the_first(self, tmp_path):
        # Under a systemd user manager the first `.service` in the path is
        # `user@1000.service` — the user manager's own cgroup, which is
        # delegated and writable. Taking it would put task cgroups beside every
        # other unit that user runs, outside this unit's accounting, and the
        # kernel-is-the-probe rule would return a false positive because the
        # mkdir and the memory.max write both succeed there. The sweep would
        # then be deleting `task-*` out of a directory it does not own.
        proc = _proc_tree(
            tmp_path,
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "istota.service/supervisor\n",
        )
        cg = tmp_path / "sys" / "fs" / "cgroup"
        unit = (
            cg / "user.slice" / "user-1000.slice" / "user@1000.service"
            / "app.slice" / "istota.service"
        )
        (unit / "supervisor").mkdir(parents=True)

        assert task_cgroup.resolve_root(proc_root=proc, cgroup_root=cg) == unit

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


@pytest.mark.integration
class TestAgainstARealCgroupFs:
    """The one test that can disagree with the fixture.

    Every other test here runs against a `tmp_path` tree where the cgroup
    interface files are ordinary files. That difference is not cosmetic — it is
    why `rmdir` needs the `_kernelish_rmdir` shim above, and it is exactly the
    kind of gap that lets a fixture agree with an implementation while the
    kernel does not. The module's whole correctness argument rests on kernel
    behaviour ("a successful `memory.max` write proves the controller is
    delegated"), so it is worth one test that asks the kernel.

    Deselected by default (`-m 'not integration'`) and skipped unless it finds a
    writable delegated subtree, which in practice means running it as the
    service user on a deployed host:

        uv run pytest tests/test_task_cgroup.py -m integration -n0
    """

    @pytest.fixture
    def live_root(self):
        if not sys.platform.startswith("linux"):
            pytest.skip("cgroup v2 is Linux-only")
        root = task_cgroup.resolve_root()
        if root is None:
            pytest.skip("no delegated unit cgroup (needs Delegate= on the unit)")
        reason = task_cgroup.probe(root)
        if reason is not None:
            pytest.skip(f"delegation not usable here: {reason}")
        return root

    def test_create_place_and_destroy_against_the_kernel(self, live_root):
        import subprocess

        path = task_cgroup.create(999999, DEFAULT_LIMITS, attempt=0, root=live_root)
        assert path is not None, "probe said this would work"
        try:
            # The kernel made these; the fixture cannot prove that.
            assert (path / "memory.max").read_text().strip() == str(
                DEFAULT_LIMITS.memory_max_mb * 1024 * 1024
            )
            child = subprocess.Popen(["sleep", "30"], start_new_session=True)
            try:
                assert task_cgroup.place(child.pid, path) is True
                members = (path / "cgroup.procs").read_text().split()
                assert str(child.pid) in members

                # destroy must kill it: rmdir alone would get EBUSY forever.
                assert task_cgroup.destroy(path) is True
                assert child.wait(timeout=10) is not None
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            assert not path.exists()
        finally:
            task_cgroup.destroy(path)
