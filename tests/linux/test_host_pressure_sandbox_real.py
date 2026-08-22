"""What `read_sandbox_shm` sees when a real bubblewrap sandbox is running.

`tests/test_host_pressure.py` drives every one of these paths against a fixture
`/proc` tree it wrote itself. That is the right shape for the parsing and the
rendering, and it is structurally unable to check the one assumption the whole
feature rests on: that the pid recorded in `tasks.worker_pid` is not the pid
whose mount namespace holds the sandbox tmpfs.

It is not. `worker_pid` is what `subprocess.Popen` returned, which is the outer
`bwrap` — the privileged monitor that stays in the daemon's own mount
namespace. bwrap forks during namespace setup and the child is what owns the
private `/` and `/tmp`. The first version of this fix read the recorded pid
directly, and against a hand-written fixture every test passed while the
production behaviour would have been to restate the *host's* tmpfs mounts, once
per running task, labelled as that task's sandbox usage — double-counting
against the `tmpfs` section directly above and blaming a task for host memory.
The same fork is why per-task cgroup placement had to move into `preexec_fn`
(ISSUE-285).

So these tests spawn a real bwrap, write a known number of bytes into its
`/tmp`, and assert the bytes come back. Nothing short of executing the sandbox
can establish that.

Run them with `scripts/test-linux.sh`. They carry the `linux` marker, which
pyproject's addopts deselects.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from istota import host_pressure
from istota.executor import _bwrap_available

pytestmark = pytest.mark.linux

# Big enough that no rounding or bookkeeping noise could produce it by
# accident, small enough to write in well under a second.
PAYLOAD_MB = 64


def _unavailable(reason):
    """Skip — unless we are inside the runner, where a skip is the bug.

    Mirrors `test_sandbox_real.py`. `scripts/test-linux.sh` sets
    ISTOTA_LINUX_TIER=1 and exists precisely to make this path execute; a
    quiet skip in there would let the driver exit 0 having asserted nothing,
    which is the silent non-execution the tier was built to end.
    """
    if os.environ.get("ISTOTA_LINUX_TIER") == "1":
        pytest.fail(f"running under scripts/test-linux.sh, where this must not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


@pytest.fixture
def sandbox():
    """A live bwrap with a private tmpfs `/tmp` holding PAYLOAD_MB of bytes.

    Yields the `Popen`, whose `.pid` is the outer monitor — deliberately, since
    that is exactly what the daemon records and hands to `read_sandbox_shm`.
    """
    script = (
        f"dd if=/dev/zero of=/tmp/ballast bs=1M count={PAYLOAD_MB} 2>/dev/null; "
        "echo ready; "
        "sleep 120"
    )
    proc = subprocess.Popen(
        [
            "bwrap", "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
            "--", "/bin/sh", "-c", script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Block until the payload is written, so the assertions are not racing
        # `dd`. A bwrap that failed to start closes stdout and gives "".
        line = proc.stdout.readline()
        if line.strip() != "ready":
            proc.kill()
            _unavailable(f"bwrap did not start: {proc.stderr.read()[:400]}")
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


class TestAgainstARealSandbox:
    def test_the_recorded_pid_is_not_the_one_holding_the_tmpfs(self, sandbox):
        """The assumption the fixture-tree tests cannot check, asserted directly.

        If this ever fails, the descent in `find_sandboxed_pid` has become
        unnecessary — but the rest of the module is written on the strength of
        it, so it should fail loudly rather than quietly become redundant.
        """
        own = os.readlink("/proc/self/ns/mnt")
        outer = os.readlink(f"/proc/{sandbox.pid}/ns/mnt")

        assert outer == own, (
            "the pid Popen returned is expected to share the daemon's mount "
            "namespace; if it no longer does, revisit find_sandboxed_pid"
        )

    def test_the_descent_finds_a_pid_in_its_own_namespace(self, sandbox):
        own = os.readlink("/proc/self/ns/mnt")

        inner = host_pressure.find_sandboxed_pid(sandbox.pid, own)

        assert inner is not None, "no descendant of the outer bwrap was sandboxed"
        assert os.readlink(f"/proc/{inner}/ns/mnt") != own

    def test_the_sandbox_tmpfs_is_reported_against_the_task_id(self, sandbox):
        """The end-to-end claim of ISSUE-286: bytes written inside a task's
        sandbox come back attributed to that task."""
        rows = host_pressure.read_sandbox_shm([(309264, sandbox.pid)])

        tmp_rows = [r for r in rows if r.mount_point == "/tmp" and r.available]
        assert tmp_rows, f"no readable /tmp row; got {[(r.mount_point, r.detail) for r in rows]}"
        row = tmp_rows[0]
        assert row.name == "309264"
        assert row.used_bytes >= PAYLOAD_MB * 1024 * 1024

    def test_that_usage_is_invisible_to_the_host_mount_table(self, sandbox):
        """The other half of the diagnosis: this is memory no amount of reading
        the daemon's own mounts would have found, which is why it landed in the
        residue with nothing to name it."""
        host_tmpfs = host_pressure.read_tmpfs_usage()

        payload_bytes = PAYLOAD_MB * 1024 * 1024
        assert not any(
            m.mount_point == "/tmp" and m.used_bytes >= payload_bytes
            for m in host_tmpfs
        ), "the sandbox payload showed up in the host mount table; fixture is not isolating"

    def test_the_snapshot_names_the_task(self, sandbox):
        text = host_pressure.snapshot(task_pids=[(309264, sandbox.pid)], containers=[])

        sandbox_lines = [
            ln.strip() for ln in text.splitlines() if ln.strip().startswith("sandbox ")
        ]
        assert any(
            ln.startswith("sandbox task=309264 mount=/tmp used_kb=")
            and int(ln.split("used_kb=")[1].split()[0]) >= PAYLOAD_MB * 1024
            for ln in sandbox_lines
        ), f"no /tmp row carrying the payload; got {sandbox_lines}"

    def test_an_exited_task_is_reported_unavailable_not_as_the_host(self, sandbox):
        """The failure mode that matters. Once the sandbox is gone there is no
        descendant in another namespace, and the row must say so rather than
        fall back to the recorded pid and print the host's mounts."""
        sandbox.kill()
        sandbox.wait(timeout=10)
        # The pid is reaped; give the kernel a moment to drop /proc/<pid>.
        for _ in range(50):
            if not Path(f"/proc/{sandbox.pid}").exists():
                break
            time.sleep(0.1)

        rows = host_pressure.read_sandbox_shm([(309264, sandbox.pid)])

        assert len(rows) == 1
        assert rows[0].available is False
        assert rows[0].used_bytes == 0
