"""The istota container's run contract, observed in a running stack.

The lean stack runs the shipped image with the shipped run contract (held line
for line to `docker/docker-compose.yml` by
`tests/test_container_security_profiles.py`) and the shipped root phase, so
each class here witnesses one row of the one-deployment-shape spec's security
parity matrix against the artifact rather than against its source.

Every assertion is about something only the mechanism produces, and each row
has a negative control in `scripts/test-smoke-negative-control.sh` that breaks
exactly that mechanism and requires the named tests here to go red:

- row 4, a task's own cgroup: the task's pid is in `task-<id>-<attempt>/`, the
  limits are the configured ones, and a process past `memory.max` is
  OOM-killed and the daemon says so. Without delegation there is no such
  directory to find.
- row 5, the daemon unprivileged: uid 10001 and every capability set empty on
  pid 1, which a root daemon cannot show.
- row 6, a read-only root: a uid-0 write fails with EROFS, the one errno a
  permission check never produces.
- row 8, no Docker API: no socket at any path, none mounted, no DOCKER_HOST.
- row 16, a task's syscall surface: bpf, keyctl, add_key, userfaultfd and
  perf_event_open are EPERM from inside a live sandbox. The arguments are the
  Stage 1 spike's, chosen so that a kernel that let the call through answers
  something else (EINVAL, EFAULT, or success).

`stack.exec` drops to uid 10001 through `istota-drop`, as every exec path into
this container must; the reads that need uid 0 say `user="0"`.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.support import parity

pytestmark = pytest.mark.smoke

ISTOTA_UID = "10001"
CGROUP = "/sys/fs/cgroup"
#: The shipped defaults (`SchedulerConfig`), which the lean render leaves alone.
CONFIGURED_MEMORY_MAX = str(2048 * 1024 * 1024)
CONFIGURED_PIDS_MAX = "512"


def _ok(result, what: str) -> str:
    assert result.returncode == 0, (
        f"{what} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _status_fields(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    return fields


@parity.witness(5)
class TestTheDaemonIsUnprivileged:
    """Row 5, the daemon half: uid 10001, nothing in any capability set.

    The wrapper half (the VM's `istota` wrapper) lands with the wrapper.
    """

    def test_pid_1_is_the_scheduler(self, stack):
        cmdline = _ok(stack.exec(["sh", "-c", "tr '\\0' ' ' < /proc/1/cmdline"]), "read cmdline")

        assert "/app/.venv/bin/istota-scheduler" in cmdline, cmdline

    def test_the_daemon_runs_as_10001_with_every_capability_set_empty(self, stack):
        status = _status_fields(_ok(stack.exec(["cat", "/proc/1/status"]), "read status"))

        assert status["Uid"].split() == [ISTOTA_UID] * 4, status["Uid"]
        assert status["Gid"].split() == [ISTOTA_UID] * 4, status["Gid"]
        for capset in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert status[capset] == "0000000000000000", f"{capset}={status[capset]}"
        assert status["NoNewPrivs"] == "1"
        assert status["Seccomp"] == "2", "the daemon runs without a seccomp filter"

    def test_nothing_the_daemon_wrote_is_owned_by_root(self, stack):
        # /data/config is the harness's read-only bind of a host directory and
        # is not the daemon's.
        found = _ok(
            stack.exec(
                ["sh", "-c", "find /data -path /data/config -prune -o -user 0 -print; echo END"],
                user="0",
            ),
            "find root-owned files",
        )

        assert found.strip().endswith("END"), found
        assert found.replace("END", "").strip() == "", f"root-owned under /data:\n{found}"


@parity.witness(6)
class TestTheRootFilesystemIsReadOnly:
    """Row 6: the daemon's write scope is its volumes and tmpfs.

    Probed as uid 0, because uid 10001 is refused by permission bits whether or
    not the root is read-only, and the errno is the evidence: EROFS comes from
    the mount and from nothing else.
    """

    @pytest.mark.parametrize("directory", ["/app", "/usr", "/etc"])
    def test_a_root_write_is_refused_by_the_mount(self, stack, directory):
        result = stack.exec(
            ["sh", "-c", f"touch {directory}/.istota-write-probe 2>&1; echo EXIT=$?"],
            user="0",
        )

        assert "EXIT=0" not in result.stdout, f"{directory} is writable as root"
        assert "Read-only file system" in result.stdout, result.stdout

    def test_the_daemons_root_mount_is_read_only(self, stack):
        mountinfo = _ok(stack.exec(["cat", "/proc/1/mountinfo"]), "read mountinfo")
        root = [line.split() for line in mountinfo.splitlines() if line.split()[4] == "/"]

        assert root, mountinfo
        assert "ro" in root[-1][5].split(","), root[-1]


@parity.witness(8)
class TestNoDockerApi:
    """Row 8: no Docker socket anywhere in the container, and no DOCKER_HOST."""

    def test_no_docker_socket_at_any_path(self, stack):
        found = _ok(
            stack.exec(
                ["sh", "-c",
                 "find / \\( -path /proc -o -path /sys \\) -prune -o "
                 "\\( -type s -o -type f \\) -name 'docker*.sock' -print 2>/dev/null; echo END"],
                user="0",
            ),
            "search for a docker socket",
        )

        assert found.strip().endswith("END"), found
        assert found.replace("END", "").strip() == "", f"docker socket in the container:\n{found}"

    def test_nothing_is_mounted_from_a_docker_socket(self, stack):
        mountinfo = _ok(stack.exec(["cat", "/proc/1/mountinfo"]), "read mountinfo")

        assert "docker.sock" not in mountinfo

    def test_the_daemon_has_no_docker_host(self, stack):
        environ = _ok(stack.exec(["sh", "-c", "tr '\\0' '\\n' < /proc/1/environ"]), "read environ")

        assert "ISTOTA_" in environ, "the daemon's environment came back empty"
        assert not [line for line in environ.splitlines() if line.startswith("DOCKER_HOST=")]


# Row 16's probe, run as a task's Bash tool call. Syscall numbers per
# architecture; the arguments are the spike's (vm/spike/RESULTS.md).
SYSCALL_PROBE = r"""
echo SYSCALL_PROBE_BEGIN
echo "dbdir=$(stat -f -c %T /data/db 2>&1)"
python3 - <<'PY'
import ctypes, errno, os, platform
NR = {"x86_64": {"bpf": 321, "keyctl": 250, "add_key": 248, "userfaultfd": 323, "perf_event_open": 298},
      "aarch64": {"bpf": 280, "keyctl": 219, "add_key": 217, "userfaultfd": 282, "perf_event_open": 241}}[platform.machine()]
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def call(name, *args):
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(NR[name]), *args)
    if rc >= 0:
        if name == "userfaultfd":
            os.close(rc)
        return "ok"
    return errno.errorcode.get(ctypes.get_errno(), "?")
attr = ctypes.create_string_buffer(128)
print("bpf=" + call("bpf", ctypes.c_int(0), attr, ctypes.c_uint(128)))
print("keyctl=" + call("keyctl", ctypes.c_int(0), ctypes.c_int(-3), ctypes.c_int(0)))
print("add_key=" + call("add_key", ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_size_t(0), ctypes.c_int(-3)))
print("userfaultfd=" + call("userfaultfd", ctypes.c_int(os.O_CLOEXEC | 1)))
print("perf_event_open=" + call("perf_event_open", ctypes.c_void_p(0), ctypes.c_int(0), ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_ulong(0)))
PY
echo SYSCALL_PROBE_END
"""

SYSCALL_SCRIPT = [
    {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": SYSCALL_PROBE}}]},
    {"text": "I called the syscalls"},
]


def _marked(stack, begin: str, end: str) -> str:
    transcript = stack.endpoint.transcript()
    start = transcript.find(begin)
    stop = transcript.find(end, start + 1)
    if start < 0 or stop < 0:
        raise AssertionError(
            f"{begin} never reached the model, so the Bash tool did not run\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[start:stop]


@parity.witness(16)
class TestATasksSyscallSurface:
    """Row 16: the shipped seccomp profile reaches every sandbox.

    Read together with the sandbox's own mark: the database directory is the
    empty tmpfs `build_bwrap_cmd` masks it with, so the calls were made from
    inside a live bwrap namespace, which the profile also has to permit.
    """

    @pytest.mark.script(SYSCALL_SCRIPT)
    def test_the_denied_calls_are_eperm_inside_a_live_sandbox(self, stack):
        task_id = stack.submit("call some syscalls")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = _marked(stack, "SYSCALL_PROBE_BEGIN", "SYSCALL_PROBE_END")

        assert "dbdir=tmpfs" in observed, f"the probe did not run in the sandbox:\n{observed}"
        for call in ("bpf", "keyctl", "add_key", "userfaultfd", "perf_event_open"):
            assert f"{call}=EPERM" in observed, f"{call} was not refused:\n{observed}"


# Row 4: the task prints its own cgroup, waits while the test reads and then
# tightens the limits from outside, and allocates past them.
CGROUP_PROBE = r"""
echo CGROUP_PROBE_BEGIN
echo "own=$(cat /proc/self/cgroup)"
sleep 20
python3 -c "b = bytearray(512 << 20)
for i in range(0, len(b), 4096): b[i] = 1
print('survived')"
echo "hog_exit=$?"
echo CGROUP_PROBE_END
"""

CGROUP_SCRIPT = [
    {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": CGROUP_PROBE}}]},
    {"text": "I ran the allocation"},
]


@parity.witness(4)
class TestATaskIsInItsOwnCgroup:
    """Row 4: per-task limits, enforced by the kernel in the container.

    The configured `memory.max` is read while the task runs. Driving a process
    past 2 GiB on a test host with swap would thrash the host before the kernel
    killed anything, so the test then lowers that one task's `memory.max` and
    `memory.swap.max` from outside, which the daemon's uid may do because the
    root phase delegated the subtree to it, and the task allocates past the new
    limit. The kill, and the daemon's report of it, are the kernel's and the
    executor's own.
    """

    def _wait_for_group(self, stack, task_id: int, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listing = stack.exec(["sh", "-c", f"ls -d {CGROUP}/task-{task_id}-* 2>/dev/null"]).stdout
            groups = listing.split()
            if groups:
                procs = stack.exec(["cat", f"{groups[0]}/cgroup.procs"]).stdout.split()
                if len(procs) >= 2:
                    return groups[0]
            time.sleep(0.5)
        raise AssertionError(
            f"no populated {CGROUP}/task-{task_id}-* appeared within {timeout}s\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )

    def test_doctor_reports_the_delegated_root_ok(self, stack):
        report = {row["name"]: row for row in stack.doctor()}
        row = report.get("security.task_cgroups")

        assert row is not None, sorted(report)
        assert row["status"] == "ok", row
        assert CGROUP in row["detail"], row

    @pytest.mark.script(CGROUP_SCRIPT)
    def test_a_task_is_placed_limited_and_killed_past_its_limit(self, stack):
        task_id = stack.submit("allocate some memory")
        group = self._wait_for_group(stack, task_id)

        memory_max = _ok(stack.exec(["cat", f"{group}/memory.max"]), "read memory.max").strip()
        pids_max = _ok(stack.exec(["cat", f"{group}/pids.max"]), "read pids.max").strip()
        assert memory_max == CONFIGURED_MEMORY_MAX, memory_max
        assert pids_max == CONFIGURED_PIDS_MAX, pids_max

        current = int(_ok(stack.exec(["cat", f"{group}/memory.current"]), "read memory.current"))
        tightened = current + (128 << 20)
        _ok(
            stack.exec(["sh", "-c",
                        f"echo {tightened} > {group}/memory.max; "
                        f"[ ! -e {group}/memory.swap.max ] || echo 0 > {group}/memory.swap.max"]),
            "tighten the task's limits",
        )

        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=240)
        observed = _marked(stack, "CGROUP_PROBE_BEGIN", "CGROUP_PROBE_END")

        own = re.search(r"own=0::(/\S*)", observed)
        assert own and re.fullmatch(rf"/task-{task_id}-\d+", own.group(1)), observed
        assert own.group(1) == group[len(CGROUP):], (own.group(1), group)
        # A line of its own: the shell echoes the killed command's source,
        # which contains the word.
        assert not re.search(r"^survived$", observed, re.M), (
            f"the allocation outlived its limit:\n{observed}"
        )
        assert "hog_exit=137" in observed, observed
        logs = stack.logs(400)
        assert f"task {task_id}: " in logs and "OOM-killed inside the task's own cgroup" in logs, logs
