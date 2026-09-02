"""The tool server's pid, its cgroup, and how it ends — against the kernel.

Four claims that cannot be made anywhere else:

- the pid the daemon records is the outer bwrap process, and it is live;
- the server **and a Bash grandchild** are members of the task cgroup, which is
  the property the per-call placement could never have (bwrap forks during
  namespace setup, so a pid moved after the spawn leaves everything it already
  forked outside the group for good — ISSUE-285);
- cancellation and shutdown kill every descendant, including one a command
  backgrounded;
- and `/tmp` and a background process persist *across* two tool calls, which is
  the behaviour change one namespace per attempt buys and which a per-call
  sandbox threw away every time.

The EOF and malformed-frame cases are asserted in `tests/test_tool_server.py`,
which can make them on any host; what is here is only what needs a kernel.

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from istota import db, task_cgroup
from istota.config import SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd
from istota.session.tools import hello_payload, start_tool_server

from ..support.cgroups import live_cgroup_task_id
from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _q(path):
    return shlex.quote(str(path))


# --------------------------------------------------------------------------- #
# Shell probes. See the note in `test_tool_server_real.py`:
# `tests/test_linux_probe_scripts.py` runs each of these under /bin/sh against
# a present and an absent tree, because a probe whose failure mode is silence
# passes vacuously and this tier cannot be run from a developer machine.
# --------------------------------------------------------------------------- #


def background_probe(marker) -> str:
    """Start a detached writer, print `BGPID=<pid>`, return at once.

    `setsid` is deliberately *not* used: the point is that the process stays in
    the server's cgroup and in its process group, so the kill path reaches it.

    The marker is a counter rather than `date +%s`, because a caller asserting
    that the writer is *still running* compares two reads and a timestamp in
    whole seconds makes that a race: the reads below are 0.8s apart, so roughly
    one run in five saw the same second twice and reported a live process as
    dead. A counter changes on every tick whatever the clock says.
    """
    return (
        f'( n=0; while true; do n=$((n+1)); echo "$n" > {_q(marker)}; sleep 0.2; done )'
        f' >/dev/null 2>&1 & '
        f'echo "BGPID=$!"'
    )


def read_back_probe(marker) -> str:
    """`MARKER=<contents>` or `MARKER=MISSING`, printed either way."""
    return (
        f'if [ -f {_q(marker)} ]; then echo "MARKER=$(cat {_q(marker)})"; '
        f'else echo "MARKER=MISSING"; fi'
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def layout(tmp_path, make_config):
    db_dir = tmp_path / "app" / "data"
    db_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text("db")
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    return make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=tmp_path / "app" / "moduledbs",
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True),
    )


@pytest.fixture
def user_temp(layout):
    d = layout.temp_dir / "alice"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


@pytest.fixture
def live_cgroup():
    """A real delegated cgroup, or a skip.

    Same resolution as `tests/test_task_cgroup_placement.py`: a container's
    `/proc/self/cgroup` is `0::/` under the default private namespace, so
    `resolve_root` answers None however writable the tree is —
    `scripts/test-linux.sh` builds a delegated subtree by hand and names it in
    `ISTOTA_TEST_CGROUP_ROOT`.
    """
    env_root = os.environ.get("ISTOTA_TEST_CGROUP_ROOT")
    root = Path(env_root) if env_root else task_cgroup.resolve_root()
    if root is None:
        _unavailable("no delegated unit cgroup (needs Delegate= on the unit)")
    reason = task_cgroup.probe(root)
    if reason is not None:
        _unavailable(f"delegation not usable here: {reason}")
    path = task_cgroup.create(
        live_cgroup_task_id(),
        task_cgroup.CgroupLimits(memory_max_mb=2048, pids_max=512, cpu_max_percent=0),
        attempt=0,
        root=root,
    )
    assert path is not None, "probe said this would work"
    try:
        yield path
    finally:
        task_cgroup.destroy(path)


def _wrap_for(layout, task, user_temp):
    def _wrap(cmd):
        wrapped = build_bwrap_cmd(
            cmd, layout, task, False, [], user_temp, profile=SandboxProfile.NATIVE,
        )
        assert wrapped[0] == "bwrap", "sandbox unavailable — nothing below is meaningful"
        return wrapped

    return _wrap


def _hello(user_temp):
    return hello_payload(
        cwd=user_temp,
        subprocess_env={"PATH": "/usr/bin:/bin", "HOME": str(user_temp)},
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=user_temp,
        bash_timeout_seconds=60,
        max_output_bytes=30_000,
        max_read_lines=2000,
        max_read_bytes=25_000_000,
        bash_spill_full_output=True,
    )


def _text(result):
    return "".join(getattr(b, "text", "") for b in result.content)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie answers signal 0; ask /proc for the state instead.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    tail = stat.rpartition(")")[2].split()
    return bool(tail) and tail[0] != "Z"


# --------------------------------------------------------------------------- #


class TestThePidIsRecorded:
    def test_on_pid_reports_the_live_outer_bwrap(self, layout, task, user_temp):
        """`worker_pid` is what `!stop`, the web cancel endpoint and
        `host_pressure.read_sandbox_shm` reach for. This brain recorded none at
        all before the server existed, so every native task carried 0."""
        seen = []

        async def _go():
            server = await start_tool_server(
                _hello(user_temp),
                sandbox_wrap=_wrap_for(layout, task, user_temp),
                on_pid=seen.append,
            )
            try:
                assert seen == [server.pid]
                assert _alive(server.pid)
                cmdline = Path(f"/proc/{server.pid}/cmdline").read_bytes()
                # The outer process is bwrap, not the interpreter: that is the
                # pid whose descendants `find_sandboxed_pid` walks.
                assert b"bwrap" in cmdline, cmdline[:120]
            finally:
                await server.aclose()

        asyncio.run(_go())
        assert not _alive(seen[0]), "the server outlived its own shutdown"


class TestTheWholeTreeIsInTheTaskCgroup:
    def test_the_server_and_a_bash_grandchild_are_members(
        self, layout, task, user_temp, live_cgroup,
    ):
        """The claim the per-call placement could not make.

        Membership is read from the *host's* `cgroup.procs`, not from inside
        the namespace: under `--unshare-pid` the command's own `$$` is a
        namespace-local number that means nothing here, and comparing it would
        be a test of pid namespaces rather than of containment.
        """
        async def _go():
            server = await start_tool_server(
                _hello(user_temp),
                sandbox_wrap=_wrap_for(layout, task, user_temp),
                task_cgroup_path=live_cgroup,
            )
            try:
                before = (live_cgroup / "cgroup.procs").read_text().split()
                marker = user_temp / "bg.txt"
                out = _text(await server.call(
                    "Bash", "1", {"command": background_probe(marker)}, None, None
                ))
                # Give the forks a moment to land.
                await asyncio.sleep(0.5)
                after = (live_cgroup / "cgroup.procs").read_text().split()
                return out, before, after, server.pid
            finally:
                await server.aclose()

        out, before, after, pid = asyncio.run(_go())
        assert "BGPID=" in out, out
        assert str(pid) in before, (
            "the outer bwrap is not a member — placement did not engage, and "
            "everything below is about a cgroup nothing is in"
        )
        # More than the one process: the server behind bwrap, the shell, and
        # the backgrounded loop all inherited membership at fork. An exact set
        # is not assertable (pids are namespace-local inside), so the count is
        # what distinguishes inheritance from a lone placed process.
        assert len(after) > len(before), (after, before)

    def test_the_members_are_gone_after_shutdown(
        self, layout, task, user_temp, live_cgroup,
    ):
        """The other half: `aclose` kills the *group*, so a backgrounded
        process does not outlive the attempt. Without this the test above would
        pass on a server that leaked its whole tree."""
        async def _go():
            server = await start_tool_server(
                _hello(user_temp),
                sandbox_wrap=_wrap_for(layout, task, user_temp),
                task_cgroup_path=live_cgroup,
            )
            await server.call(
                "Bash", "1", {"command": background_probe(user_temp / "bg.txt")}, None, None
            )
            await asyncio.sleep(0.3)
            await server.aclose()

        asyncio.run(_go())
        for _ in range(50):
            members = (live_cgroup / "cgroup.procs").read_text().split()
            if not members:
                break
            time.sleep(0.1)
        assert (live_cgroup / "cgroup.procs").read_text().split() == []


class TestPersistenceAcrossToolCalls:
    def test_tmp_survives_between_two_calls(self, layout, task, user_temp):
        """`/tmp` is a `--tmpfs` inside the namespace. With one namespace per
        attempt it is the same tmpfs on the second call; with one per call it
        was a fresh one every time and everything written was lost."""
        async def _go():
            server = await start_tool_server(
                _hello(user_temp), sandbox_wrap=_wrap_for(layout, task, user_temp)
            )
            try:
                await server.call(
                    "Bash", "1", {"command": "echo kept > /tmp/probe"}, None, None
                )
                return _text(await server.call(
                    "Bash", "2", {"command": read_back_probe(Path("/tmp/probe"))},
                    None, None,
                ))
            finally:
                await server.aclose()

        assert "MARKER=kept" in asyncio.run(_go())

    def test_a_backgrounded_process_survives_between_two_calls(
        self, layout, task, user_temp,
    ):
        """Real, observable, and the same as ClaudeCodeBrain's behaviour: a
        server started in one call is still answering in the next. Under the
        per-call sandbox it died with the namespace that spawned it."""
        marker = user_temp / "heartbeat.txt"

        async def _go():
            server = await start_tool_server(
                _hello(user_temp), sandbox_wrap=_wrap_for(layout, task, user_temp)
            )
            try:
                await server.call(
                    "Bash", "1", {"command": background_probe(marker)}, None, None
                )
                await asyncio.sleep(0.4)
                first = _text(await server.call(
                    "Bash", "2", {"command": read_back_probe(marker)}, None, None
                ))
                await asyncio.sleep(0.8)
                second = _text(await server.call(
                    "Bash", "3", {"command": read_back_probe(marker)}, None, None
                ))
                return first, second
            finally:
                await server.aclose()

        first, second = asyncio.run(_go())
        assert "MARKER=MISSING" not in first, first
        assert "MARKER=MISSING" not in second, second
        assert first != second, (
            "the background process stopped between the calls — it was killed "
            "with its call rather than living for the attempt"
        )


class TestShutdownReachesEveryDescendant:
    def test_a_backgrounded_process_does_not_outlive_the_attempt(
        self, layout, task, user_temp,
    ):
        """No cgroup needed for this one, which matters: `task_cgroup_enabled`
        is a config knob and a deployment without `Delegate=` still must not
        leak a process per task."""
        marker = user_temp / "outlives.txt"

        async def _go():
            server = await start_tool_server(
                _hello(user_temp), sandbox_wrap=_wrap_for(layout, task, user_temp)
            )
            await server.call(
                "Bash", "1", {"command": background_probe(marker)}, None, None
            )
            await asyncio.sleep(0.5)
            await server.aclose()

        asyncio.run(_go())
        assert marker.exists(), "the background process never ran — nothing was proven"
        first = marker.read_text()
        time.sleep(1.5)
        assert marker.read_text() == first, (
            "the backgrounded process is still writing after shutdown"
        )

    def test_the_native_argv_carries_die_with_parent(self, layout, task, user_temp):
        """The backstop under the graceful stop, and the only thing between a
        *crashed* daemon and one orphaned sandbox per in-flight task.

        Asserted on the argv rather than by killing a daemon, and the reason is
        worth stating so nobody "strengthens" it later: the flag's behaviour is
        bubblewrap's, exercised on every task on the deployed host, and a test
        that drove it would have to spawn a second interpreter, rebuild this
        config inside it, and race a kill against a namespace teardown — three
        moving parts around a claim whose only istota-side half is whether the
        flag reaches the command line. `tests/test_sandbox.py` asserts the same
        flag from darwin; here it is asserted on the argv the tool server is
        actually spawned with.
        """
        captured = {}

        def _wrap(cmd):
            captured["argv"] = _wrap_for(layout, task, user_temp)(cmd)
            return captured["argv"]

        async def _go():
            server = await start_tool_server(_hello(user_temp), sandbox_wrap=_wrap)
            await server.aclose()

        asyncio.run(_go())
        assert "--die-with-parent" in captured["argv"]
        assert "--unshare-pid" in captured["argv"]
