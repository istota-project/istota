"""ISSUE-285 — a task's whole process tree is in the cgroup, not just its root.

``place()`` writes a pid to ``cgroup.procs`` after ``Popen`` has returned.
cgroup v2 membership is inherited at ``fork`` and moving a parent does not move
children that already exist, so anything the child forked in the window between
spawn and placement keeps the cgroup the daemon spawned it from — permanently.
For ``sleep 30`` the window is empty. For ``bwrap``, which forks its inner
process while setting up namespaces, it is lost every time: on the deployment
server a task cgroup with a 2 GiB cap held one sleeping ``bwrap`` and
``memory.current=0`` eighteen minutes in, while the tree doing the work sat in
the daemon's own leaf under no limit at all.

The fix is to place the child *before* it execs, from ``preexec_fn``, so the
process is already a member when it forks and every descendant inherits it.
These tests cover that property at the three levels it can be broken:

- the module's own ``placement()`` / ``verify_placement()`` (portable);
- the two spawn paths that carry it — ClaudeCodeBrain's streaming ``Popen`` and
  NativeBrain's Bash tool (portable, real subprocesses, fake cgroup dir);
- fork inheritance itself, against a real cgroup2fs (``linux``).

The portable tests can show that placement happens in the child before exec.
Only the kernel can show that a *grandchild* forked afterwards is a member,
which is the property the original code silently lacked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import task_cgroup
from istota.brain._types import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain


@pytest.fixture(autouse=True)
def _fresh_log_state():
    task_cgroup._reset_log_state()
    yield
    task_cgroup._reset_log_state()


def _unavailable(reason: str):
    """Skip — unless something promised us a cgroup, where a skip is the bug.

    Same rule as ``tests/linux/test_sandbox_real.py``, keyed one notch
    narrower. That file keys on ``ISTOTA_LINUX_TIER=1`` because every container
    the driver starts can run bwrap. A delegated cgroup is not like that: it
    needs ``SYS_ADMIN`` and a writable cgroup2 mount, and a Docker setup
    without them is a limitation of the machine rather than a defect in the
    tree. Hard-failing there would make the whole Linux tier unusable for
    something these tests are only one part of.

    So the promise is what is checked. ``ISTOTA_TEST_CGROUP_ROOT`` is set by
    ``scripts/test-linux.sh`` only after it has actually built the subtree; if
    it is set and these tests still cannot run, the setup broke and saying so
    is the point. Unset means nobody claimed anything, and a skip is honest.
    """
    if os.environ.get("ISTOTA_TEST_CGROUP_ROOT"):
        pytest.fail(
            f"ISTOTA_TEST_CGROUP_ROOT was set, so this must not skip: {reason}"
        )
    pytest.skip(reason)


@pytest.fixture
def cgroup(tmp_path: Path) -> Path:
    """A task cgroup as the kernel would present it: the interface file exists.

    ``cgroup.procs`` is made by the kernel and cannot be created by a writer, so
    a fixture that leaves it out would let a bug that opens the wrong path pass.
    """
    path = tmp_path / "task-7-0"
    path.mkdir()
    (path / "cgroup.procs").write_text("")
    return path


# ---------------------------------------------------------------------------
# placement()
# ---------------------------------------------------------------------------


class TestPlacement:
    def test_the_child_writes_itself_in_before_it_execs(self, cgroup):
        """The whole point: the write happens in the forked child, not the parent.

        ``0`` rather than a pid is what makes this provable — the parent never
        writes ``0``, so finding it in the file means the callable ran on the
        child side of the fork.
        """
        with task_cgroup.placement(cgroup) as preexec:
            assert preexec is not None
            proc = subprocess.Popen([sys.executable, "-c", ""], preexec_fn=preexec)
        assert proc.wait(timeout=30) == 0

        assert (cgroup / "cgroup.procs").read_text().strip() == "0"

    def test_a_none_path_yields_no_preexec_fn(self):
        """Fail open: no delegated subtree means spawn exactly as before."""
        with task_cgroup.placement(None) as preexec:
            assert preexec is None

    def test_a_cgroup_that_cannot_be_opened_yields_none_and_says_so(
        self, tmp_path, caplog
    ):
        with caplog.at_level("WARNING"):
            with task_cgroup.placement(tmp_path / "gone") as preexec:
                assert preexec is None

        assert "uncontained" in caplog.text

    def test_quiet_leaves_the_one_shot_key_for_a_real_task(self, tmp_path, caplog):
        """`probe` measures with this; it must not spend the key a real per-task
        failure needs, or the first thing to hit it would be silent."""
        with caplog.at_level("WARNING"):
            with task_cgroup.placement(tmp_path / "gone", quiet=True) as preexec:
                assert preexec is None
            assert caplog.text == ""

            with task_cgroup.placement(tmp_path / "gone") as preexec:
                assert preexec is None
        assert "uncontained" in caplog.text

    def test_a_write_failure_in_the_child_never_reaches_the_spawn(self, cgroup):
        """An exception out of ``preexec_fn`` makes ``Popen`` raise.

        Containment is best-effort and must never cost the task its subprocess,
        so the callable swallows its own errors.
        """
        with task_cgroup.placement(cgroup) as preexec:
            with patch("istota.task_cgroup.os.write", side_effect=OSError("nope")):
                preexec()  # must not raise

    def test_the_descriptor_is_closed_when_the_block_exits(self, cgroup):
        """A daemon spawns thousands of tasks; a leaked fd per spawn is a leak."""
        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def _open(*a, **kw):
            fd = real_open(*a, **kw)
            opened.append(fd)
            return fd

        with patch("istota.task_cgroup.os.open", side_effect=_open), patch(
            "istota.task_cgroup.os.close", side_effect=lambda fd: (closed.append(fd), real_close(fd))[1]
        ):
            with task_cgroup.placement(cgroup) as preexec:
                assert preexec is not None

        assert opened and closed == opened

    def test_the_descriptor_is_closed_even_when_the_spawn_raises(self, cgroup):
        closed: list[int] = []
        real_close = os.close

        with patch(
            "istota.task_cgroup.os.close",
            side_effect=lambda fd: (closed.append(fd), real_close(fd))[1],
        ):
            with pytest.raises(RuntimeError):
                with task_cgroup.placement(cgroup):
                    raise RuntimeError("spawn blew up")

        assert len(closed) == 1


# ---------------------------------------------------------------------------
# verify_placement()
# ---------------------------------------------------------------------------


def _fake_proc(tmp_path: Path, pid: int, state: str = "R") -> Path:
    """A ``/proc`` holding one process in ``state``. ``comm`` is deliberately
    hostile: a real one can contain spaces and brackets, and a parser that
    splits from the left reads the wrong field."""
    proc_root = tmp_path / "proc"
    (proc_root / str(pid)).mkdir(parents=True)
    (proc_root / str(pid) / "stat").write_text(f"{pid} (we ird) pro) {state} 1 1 0\n")
    return proc_root


class TestVerifyPlacement:
    def test_true_when_the_pid_is_listed(self, cgroup):
        (cgroup / "cgroup.procs").write_text("41\n42\n")
        assert task_cgroup.verify_placement(42, cgroup) is True

    def test_a_healthy_placement_is_silent(self, cgroup, caplog):
        """The warning has to be absent on the good path, not merely present on
        the bad one — a check that fires either way carries no information."""
        (cgroup / "cgroup.procs").write_text("42\n")
        with caplog.at_level("WARNING"):
            assert task_cgroup.verify_placement(42, cgroup) is True
        assert caplog.text == ""

    def test_false_and_a_warning_when_a_live_pid_is_absent(
        self, cgroup, tmp_path, caplog
    ):
        (cgroup / "cgroup.procs").write_text("41\n")
        proc_root = _fake_proc(tmp_path, 42)
        with caplog.at_level("WARNING"):
            assert (
                task_cgroup.verify_placement(42, cgroup, proc_root=proc_root) is False
            )
        assert "uncontained" in caplog.text

    def test_a_child_that_already_exited_is_not_reported(
        self, cgroup, tmp_path, caplog
    ):
        """The Bash tool spawns commands shorter-lived than the read back.

        The kernel drops a task from `cgroup.procs` at exit, so "never placed"
        and "already finished" are the same observation from here — and with the
        warning keyed per cgroup, one spurious hit would eat the key a genuine
        miss on that task needed.
        """
        (cgroup / "cgroup.procs").write_text("")
        with caplog.at_level("WARNING"):
            assert (
                task_cgroup.verify_placement(42, cgroup, proc_root=tmp_path / "proc")
                is False
            )
        assert caplog.text == ""

    def test_a_zombie_is_not_reported_either(self, cgroup, tmp_path, caplog):
        """A child that exited but has not been reaped is still in `/proc` and
        already out of the cgroup, so liveness has to mean more than existence."""
        (cgroup / "cgroup.procs").write_text("")
        proc_root = _fake_proc(tmp_path, 42, state="Z")
        with caplog.at_level("WARNING"):
            assert (
                task_cgroup.verify_placement(42, cgroup, proc_root=proc_root) is False
            )
        assert caplog.text == ""

    def test_the_warning_is_keyed_per_cgroup_not_once_per_process(
        self, tmp_path, caplog
    ):
        """`place()` states this rule for itself: a placement failure is a fact
        about *this* task, so one shared key would report the first uncontained
        task and silence every one after it."""
        proc_root = _fake_proc(tmp_path, 42)
        paths = []
        for name in ("task-1-0", "task-2-0"):
            p = tmp_path / name
            p.mkdir()
            (p / "cgroup.procs").write_text("")
            paths.append(p)

        with caplog.at_level("WARNING"):
            for p in paths:
                task_cgroup.verify_placement(42, p, proc_root=proc_root)

        assert caplog.text.count("uncontained") == 2

    def test_an_unreadable_cgroup_is_false_rather_than_a_raise(self, tmp_path):
        assert task_cgroup.verify_placement(42, tmp_path / "gone") is False

    def test_a_pid_is_matched_whole_not_as_a_substring(self, cgroup, tmp_path):
        """`4` must not match `42`. A naive `in` check on the text would."""
        (cgroup / "cgroup.procs").write_text("42\n")
        proc_root = _fake_proc(tmp_path, 4)
        assert task_cgroup.verify_placement(4, cgroup, proc_root=proc_root) is False


# ---------------------------------------------------------------------------
# The spawn paths
# ---------------------------------------------------------------------------


def _req(tmp_path: Path, **kw) -> BrainRequest:
    defaults = dict(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        model="",
        effort="",
    )
    defaults.update(kw)
    return BrainRequest(**defaults)


def _mock_process(pid=4242):
    mock = MagicMock()
    mock.stdout = iter([])
    mock.stderr = iter([])
    mock.pid = pid
    mock.returncode = None

    def _wait(*_a, **_kw):
        mock.returncode = 0
        return 0

    mock.wait.side_effect = _wait
    return mock


class TestClaudeCodeBrainPlacesBeforeExec:
    """The path ISSUE-285 was observed on: bwrap, spawned by the streaming run."""

    def test_the_spawn_carries_a_preexec_fn_that_places_the_child(
        self, tmp_path, cgroup
    ):
        with patch(
            "istota.brain.claude_code.subprocess.Popen", return_value=_mock_process()
        ) as popen:
            ClaudeCodeBrain()._execute_streaming_once(
                ["claude"], _req(tmp_path, task_cgroup=cgroup)
            )

        preexec = popen.call_args.kwargs.get("preexec_fn")
        assert preexec is not None, (
            "the child must be placed from preexec_fn; placing it after Popen "
            "returns leaves everything bwrap already forked outside the cgroup"
        )

    def test_no_preexec_fn_where_the_deployment_has_no_cgroup(self, tmp_path):
        with patch(
            "istota.brain.claude_code.subprocess.Popen", return_value=_mock_process()
        ) as popen:
            ClaudeCodeBrain()._execute_streaming_once(["claude"], _req(tmp_path))

        assert popen.call_args.kwargs.get("preexec_fn") is None

    def test_a_real_child_lands_in_the_cgroup_through_the_brains_own_spawn(
        self, tmp_path, cgroup
    ):
        """End to end through the real ``Popen``, no mock in the spawn path."""
        req = _req(tmp_path, task_cgroup=cgroup)
        ClaudeCodeBrain()._execute_streaming_once(["true"], req)

        assert (cgroup / "cgroup.procs").read_text().strip() == "0"

    def test_the_non_streaming_path_is_placed_too(self, tmp_path, cgroup):
        """`use_streaming` is `event_writer is not None`, so a deployment with
        `scheduler.event_log_enabled = false` runs every task through here. It
        never calls `on_pid` either, so before this it was the one spawn no
        placement of any kind reached — while the startup line still reported
        containment."""
        req = _req(tmp_path, task_cgroup=cgroup)
        ClaudeCodeBrain()._execute_simple_once(["true"], req)

        assert (cgroup / "cgroup.procs").read_text().strip() == "0"

    def test_an_unopenable_cgroup_is_not_verified_on_top(self, tmp_path, caplog):
        """`placement` already names the cause when it yields None. Verifying
        anyway would log a second warning for the same fact and burn the key a
        real per-task miss needs.

        Asserted on the call rather than on the warning count: `true` exits fast
        enough that the liveness check would often suppress the second warning
        by luck, which would let this pass with the guard removed.
        """
        req = _req(tmp_path, task_cgroup=tmp_path / "gone")
        with patch("istota.task_cgroup.verify_placement") as verify, caplog.at_level(
            "WARNING"
        ):
            ClaudeCodeBrain()._execute_streaming_once(["true"], req)

        verify.assert_not_called()
        assert caplog.text.count("uncontained") == 1

    def test_a_successful_placement_is_verified(self, tmp_path, cgroup):
        """The other half: the contract in `.claude/rules/scheduler.md` says each
        spawn checks its membership back, and dropping the call would otherwise
        leave the suite green."""
        req = _req(tmp_path, task_cgroup=cgroup)
        with patch("istota.task_cgroup.verify_placement") as verify:
            ClaudeCodeBrain()._execute_streaming_once(["true"], req)

        verify.assert_called_once()
        assert verify.call_args.args[1] == cgroup


class TestTheToolServerIsPlacedBeforeExec:
    """The same window, on the native brain — which now has exactly one child.

    It used to have one per Bash call, each placed from its own `preexec_fn`;
    the placement moved with the containment when the tools moved into a single
    per-attempt server. Everything the server forks — every Bash command, and
    everything those commands background — is a member by inheritance, which is
    the property the per-call form could never have for the processes bwrap
    forked during namespace setup (ISSUE-285).
    """

    @pytest.mark.asyncio
    async def test_the_server_places_itself_from_the_child_side_of_the_fork(
        self, tmp_path, cgroup
    ):
        from istota.session.tools import hello_payload, start_tool_server

        server = await start_tool_server(
            hello_payload(
                cwd=tmp_path, subprocess_env=None, read_roots=None, write_roots=None,
                write_denied_roots=(), deferred_dir=None, bash_timeout_seconds=30,
                max_output_bytes=30_000, max_read_lines=2000, max_read_bytes=25_000_000,
                bash_spill_full_output=True,
            ),
            task_cgroup_path=cgroup,
        )
        try:
            # `0`, not a pid: the child writes itself in from `preexec_fn`,
            # before it execs, so everything it goes on to fork inherits the
            # group. A pid here would mean the parent moved it after the fact,
            # which is the ordering that left the real work outside the cgroup.
            assert (cgroup / "cgroup.procs").read_text().strip() == "0"
        finally:
            await server.aclose()

    @pytest.mark.asyncio
    async def test_the_server_still_starts_when_the_cgroup_is_gone(self, tmp_path):
        """Containment is best-effort; losing it must never cost the task its
        tools. The directory does not exist, so `placement` yields None and the
        spawn is byte-identical to a deployment with no `Delegate=`."""
        from istota.session.tools import hello_payload, start_tool_server

        server = await start_tool_server(
            hello_payload(
                cwd=tmp_path, subprocess_env=None, read_roots=None, write_roots=None,
                write_denied_roots=(), deferred_dir=None, bash_timeout_seconds=30,
                max_output_bytes=30_000, max_read_lines=2000, max_read_bytes=25_000_000,
                bash_spill_full_output=True,
            ),
            task_cgroup_path=tmp_path / "gone",
        )
        try:
            result = await server.call("Bash", "c", {"command": "echo survived"}, None, None)
            assert "survived" in result.content[0].text
        finally:
            await server.aclose()


# ---------------------------------------------------------------------------
# Fork inheritance, against the kernel
# ---------------------------------------------------------------------------


@pytest.mark.linux
class TestForkInheritanceAgainstTheKernel:
    """The one test that can show the defect ISSUE-285 actually reported.

    Every portable test above proves the write lands in the child. None of them
    can prove that a process the child forks *after* placement is a member,
    because a fake cgroup directory has no membership to inherit. That is the
    property bwrap depends on and the property the old code lacked.

        uv run pytest tests/test_task_cgroup_placement.py -m integration -n0
    """

    @pytest.fixture
    def live_root(self):
        if not sys.platform.startswith("linux"):
            _unavailable("cgroup v2 is Linux-only")
        # `scripts/test-linux.sh` builds a delegated subtree by hand and names
        # it here. A container's `/proc/self/cgroup` is `0::/` under the default
        # private namespace, so `resolve_root` finds no `.service` component and
        # answers None however writable the tree is — the deployed host is the
        # only place it resolves on its own.
        env_root = os.environ.get("ISTOTA_TEST_CGROUP_ROOT")
        root = Path(env_root) if env_root else task_cgroup.resolve_root()
        if root is None:
            _unavailable("no delegated unit cgroup (needs Delegate= on the unit)")
        reason = task_cgroup.probe(root)
        if reason is not None:
            _unavailable(f"delegation not usable here: {reason}")
        return root

    def test_a_grandchild_forked_after_placement_is_a_member(self, live_root):
        limits = task_cgroup.CgroupLimits(memory_max_mb=2048, pids_max=512, cpu_max_percent=0)
        path = task_cgroup.create(999998, limits, attempt=0, root=live_root)
        assert path is not None, "probe said this would work"
        try:
            # `sh` forks `sleep` and reports its pid, which is the shape bwrap
            # has: the process that does the work is not the one we spawned.
            with task_cgroup.placement(path) as preexec:
                proc = subprocess.Popen(
                    ["sh", "-c", "sleep 30 & echo $!; wait"],
                    stdout=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    preexec_fn=preexec,
                )
            grandchild = int(proc.stdout.readline().strip())
            try:
                members = (path / "cgroup.procs").read_text().split()
                assert str(proc.pid) in members
                assert str(grandchild) in members, (
                    "the grandchild inherited the daemon's cgroup — this is "
                    "ISSUE-285, and it is what leaves a task's real work unbounded"
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
        finally:
            task_cgroup.destroy(path)

    def test_the_same_child_placed_after_the_fork_leaves_the_grandchild_out(
        self, live_root
    ):
        """The negative control: the old ordering, shown to lose the grandchild.

        Without this the test above proves only that placement works, not that
        the ordering is what makes it work — and the ordering is the entire bug.
        """
        limits = task_cgroup.CgroupLimits(memory_max_mb=2048, pids_max=512, cpu_max_percent=0)
        path = task_cgroup.create(999997, limits, attempt=0, root=live_root)
        assert path is not None
        try:
            proc = subprocess.Popen(
                ["sh", "-c", "sleep 30 & echo $!; wait"],
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            grandchild = int(proc.stdout.readline().strip())
            try:
                assert task_cgroup.place(proc.pid, path) is True
                members = (path / "cgroup.procs").read_text().split()
                assert str(proc.pid) in members
                assert str(grandchild) not in members
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
        finally:
            task_cgroup.destroy(path)
