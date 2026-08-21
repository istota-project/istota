"""A cgroup v2 group per task, so one task's process tree cannot take the host.

``MemoryHigh=`` and ``CPUWeight=`` on the unit bound the *daemon*. They do
nothing about the case the 2026-08-20 outage was: a single task running a test
suite, an ``npm ci`` or a build, whose process tree walks the machine into a
global OOM and takes an unrelated victim with it. The bwrap sandbox gives that
tree filesystem and network isolation and no resource isolation at all.

This module is the enforcement point. It puts each task's subprocesses in
``<delegation root>/task-<id>/`` with ``memory.max``, ``pids.max`` and
``cpu.max`` set, so a tree that overruns is OOM-killed *inside its own cgroup*:
one failed task instead of a host-wide event.

**Where the directory goes, and why not under the daemon's own cgroup.**
cgroup v2 forbids a non-root cgroup from both holding member processes and
enabling controllers for its children — the write to ``cgroup.subtree_control``
returns ``EBUSY``. So a ``task-<id>/`` made inside the cgroup the daemon sits
in would be created successfully and then contain no ``memory.max`` at all.
Stage 2's ``DelegateSubgroup=supervisor`` exists for this: systemd puts the
daemon in a ``supervisor/`` leaf, leaving the *unit* cgroup free to enable
controllers for its children. Task cgroups are therefore siblings of that leaf,
and :func:`resolve_root` walks up from ``/proc/self/cgroup`` to the
``.service``/``.scope`` component to find it.

**Fail open, but never fail silent.** A deployment that has not run the updated
unit file — no ``Delegate=``, no delegated subtree, an older systemd that
ignored ``DelegateSubgroup=`` — must keep working exactly as before. Every
function here returns quietly instead of raising, and :func:`create` returns
``None`` so the caller spawns as it always did. What it does not do is swallow
the difference: the reason is logged once per process, because "containment
never engaged" and "containment engaged" must not look alike in a log. That
distinction is the one the spec's own A6 notes were written to force.

The kernel is what proves the controller is there. On a real cgroup2fs the
interface files are made by the kernel and cannot be created by a writer, so a
``memory.max`` write that succeeds is evidence the memory controller is enabled
for this subtree, and one that fails is evidence it is not. There is no
separate probe, and no fixture can disagree with the host about it.

Roots are parameters, as in ``host_pressure``, so a test can point the whole
module at a tree under ``tmp_path``. stdlib-only leaf: no config import, no
logging of anything a caller did not pass in, and it never raises.
"""

from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CgroupLimits",
    "create",
    "destroy",
    "place",
    "resolve_root",
    "sweep_stale",
]

logger = logging.getLogger(__name__)

# cgroup v2 expresses `cpu.max` as "<quota_us> <period_us>". A 100 ms period is
# the kernel default and what every other writer of this file uses, so a
# percentage of one core is that many thousands of microseconds.
_CPU_PERIOD_US = 100_000

# Warnings that describe the *deployment* rather than the task: the absence of
# a delegated subtree is one fact about the host, and logging it per task would
# print it every few minutes for the life of the daemon. Keyed so a genuinely
# different reason still gets through.
_logged: set[str] = set()


def _log_once(key: str, msg: str, *args: object) -> bool:
    """Log ``msg`` at warning the first time ``key`` is seen. True if it logged."""
    if key in _logged:
        return False
    _logged.add(key)
    logger.warning(msg, *args)
    return True


def _reset_log_state() -> None:
    """Forget which one-shot warnings have fired. For tests only.

    Module-level state is per-process, and the suite runs under ``-n auto``, so
    a test that asserts "logs once" and a test that asserts "logs" would
    otherwise depend on which of them their worker ran first.
    """
    _logged.clear()


@dataclass(frozen=True)
class CgroupLimits:
    """What to write into a task's cgroup. Millisecond-cheap, all three optional.

    ``memory_max_mb`` is the one that matters: it is what turns a runaway tree
    into a failed task. ``pids_max`` bounds a fork storm. ``cpu_max_percent`` is
    a percentage of one core (200 = two cores) and ``0`` leaves CPU unbounded,
    which is the documented way to opt out of the one limit that was never the
    binding constraint in the incident.
    """

    memory_max_mb: int = 2048
    pids_max: int = 512
    cpu_max_percent: int = 200


def resolve_root(
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path | None:
    """The directory task cgroups are created in, or ``None`` where there is none.

    Reads the calling process's unified-hierarchy cgroup from
    ``/proc/self/cgroup`` and truncates at the ``.service`` / ``.scope``
    component. That component is the unit cgroup: the subtree ``Delegate=``
    chowned to the unit's ``User=``, and — with ``DelegateSubgroup=`` in effect
    — the one holding no processes of its own and therefore able to enable
    controllers for its children.

    Truncating rather than taking the path verbatim is deliberate, and it is the
    same walk :func:`istota.host_pressure.read_memory_events` makes for the same
    reason. ``/proc/self/cgroup`` reports ``…/<unit>.service/supervisor``, and a
    ``task-<id>/`` made *there* would inherit the leaf's emptiness of
    controllers rather than the unit's delegation.

    ``None`` where there is no unit component at all — a dev machine, cgroup v1,
    a container whose cgroup line names no systemd unit. In that case there is
    no subtree this module can be confident it owns, and creating directories
    in one it does not own is worse than leaving the task uncontained.
    """
    try:
        text = _read_text(Path(proc_root) / "self" / "cgroup")
        if text is None:
            return None

        # cgroup v2 puts the process on a single `0::<path>` line. A v1 line
        # (`11:memory:/…`) names a controller-specific hierarchy with no
        # `cgroup.subtree_control` in it, so matching loosely would build a
        # path that either does not exist or means something else entirely.
        rel = None
        for line in text.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
                rel = fields[2].strip()
                break
        if rel is None:
            return None

        # `..` is dropped rather than trusted. A real procfs cannot emit one,
        # but this reader takes its roots as parameters so a fixture — or a
        # container's rewritten cgroup line — can, and joining it would escape
        # `cgroup_root`.
        parts = [p for p in rel.split("/") if p and p != ".."]

        unit_end = None
        for i, part in enumerate(parts):
            if part.endswith((".service", ".scope")):
                unit_end = i + 1
                break
        if unit_end is None:
            return None

        root = Path(cgroup_root).joinpath(*parts[:unit_end])
        if not root.is_dir():
            return None
        return root
    except (OSError, ValueError):
        return None


def enable_controllers(root: Path) -> list[str]:
    """Turn on memory/pids/cpu for ``root``'s children. Returns what took.

    Best-effort and idempotent: enabling a controller that is already on is a
    successful no-op, so this can run before every ``create`` without a probe.

    Each controller is written separately rather than as one
    ``"+memory +pids +cpu"`` line, because that write is all-or-nothing — a host
    where ``cpu`` is not available above us would lose ``memory`` with it, which
    is the only one that turns a runaway tree into a failed task.
    """
    enabled = []
    control = Path(root) / "cgroup.subtree_control"
    for controller in ("memory", "pids", "cpu"):
        try:
            control.write_text(f"+{controller}\n")
            enabled.append(controller)
        except OSError:
            # EBUSY (this cgroup holds processes — no DelegateSubgroup),
            # EACCES (not delegated to us), ENOENT (no cgroup2fs here), or the
            # controller is not available from the parent. All are "no", and
            # `create` reports the consequence once it sees the limit write fail.
            continue
    return enabled


def create(
    task_id: int,
    limits: CgroupLimits,
    *,
    root: Path | None = None,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path | None:
    """Make ``task-<id>/`` with its limits written, or ``None`` if it cannot.

    ``None`` is the fail-open answer and covers every way delegation can be
    absent: no unit cgroup, a root that is not writable, a subtree with no
    memory controller. The caller spawns exactly as it would have.

    ``memory.max`` is written first and is load-bearing twice over. It is the
    limit that matters, and on a real cgroup2fs a successful write to it is the
    proof that the memory controller is actually enabled for this subtree —
    kernfs does not let a writer create the file, so it exists only if the
    kernel made it. A failure there means containment would not engage, so the
    directory is removed again and ``None`` returned rather than leaving an
    empty cgroup that looks like containment in ``systemd-cgls``.

    ``pids.max`` and ``cpu.max`` are not load-bearing in the same way: if the
    memory controller is delegated and one of the others is not, memory-only
    containment is still most of the value, so those failures are logged once
    and the cgroup is kept.
    """
    if root is None:
        root = resolve_root(proc_root=proc_root, cgroup_root=cgroup_root)
    if root is None:
        _log_once(
            "no-root",
            "task cgroups unavailable: no delegated unit cgroup found "
            "(Delegate= not applied, or not a cgroup v2 systemd host); "
            "tasks run uncontained",
        )
        return None

    path = Path(root) / f"task-{int(task_id)}"
    try:
        path.mkdir(exist_ok=True)
    except OSError as exc:
        _log_once(
            "mkdir-failed",
            "task cgroups unavailable: cannot create %s (%s); tasks run uncontained",
            path,
            exc.strerror or exc,
        )
        return None

    enable_controllers(root)

    memory_max = "max" if limits.memory_max_mb <= 0 else str(limits.memory_max_mb * 1024 * 1024)
    try:
        (path / "memory.max").write_text(memory_max + "\n")
    except OSError as exc:
        _log_once(
            "no-memory-controller",
            "task cgroups unavailable: %s has no memory.max (%s) — the memory "
            "controller is not delegated to this subtree, most likely because "
            "the daemon's own cgroup holds its processes (DelegateSubgroup= "
            "missing or unsupported); tasks run uncontained",
            path,
            exc.strerror or exc,
        )
        destroy(path)
        return None

    pids_max = "max" if limits.pids_max <= 0 else str(limits.pids_max)
    try:
        (path / "pids.max").write_text(pids_max + "\n")
    except OSError as exc:
        _log_once(
            "no-pids-controller",
            "task cgroup %s has no pids.max (%s); memory is still bounded",
            path,
            exc.strerror or exc,
        )

    # 0 means "leave CPU alone", so the file is not written at all rather than
    # written with `max`. The distinction shows up in `systemd-cgls`/`cat`, and
    # an operator reading it should see the knob they set.
    if limits.cpu_max_percent > 0:
        quota = limits.cpu_max_percent * (_CPU_PERIOD_US // 100)
        try:
            (path / "cpu.max").write_text(f"{quota} {_CPU_PERIOD_US}\n")
        except OSError as exc:
            _log_once(
                "no-cpu-controller",
                "task cgroup %s has no cpu.max (%s); memory is still bounded",
                path,
                exc.strerror or exc,
            )

    return path


def place(pid: int, path: Path) -> bool:
    """Move ``pid`` — and every process it goes on to spawn — into ``path``.

    cgroup v2 membership is inherited across ``fork``, so placing the brain's
    own child places the whole tree it builds underneath it. Writing to
    ``cgroup.procs`` moves the entire thread group, which is why the caller
    passes a child's pid and never the daemon's own.

    False rather than an exception for the ordinary misses. A process that
    exited between spawn and this write gives ``ESRCH``, which is a race the
    caller cannot avoid and does not need to hear about; anything else is
    logged once, because it means the task is running uncontained.
    """
    try:
        (Path(path) / "cgroup.procs").write_text(f"{int(pid)}\n")
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            # Already gone. Nothing to contain, nothing wrong.
            logger.debug("task cgroup: pid %d exited before placement", pid)
            return False
        _log_once(
            "place-failed",
            "task cgroup: cannot place pid %d into %s (%s); this task runs uncontained",
            pid,
            path,
            exc.strerror or exc,
        )
        return False


def destroy(path: Path) -> None:
    """Remove a task cgroup. A no-op if it is already gone.

    ``EBUSY`` means processes are still in it — a tree that outlived the brain
    subprocess. Removing a cgroup does not kill anything, so there is nothing
    useful to do here beyond leaving it: :func:`sweep_stale` takes it on the
    next daemon start, once the processes really are gone.
    """
    try:
        Path(path).rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug(
            "task cgroup: could not remove %s (%s)", path, exc.strerror or exc
        )


def sweep_stale(root: Path | None = None, *, proc_root: Path = Path("/proc"),
                cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    """Remove ``task-*`` directories left behind by a previous run. Returns the count.

    A daemon killed hard — the OOM killer, ``SIGKILL``, a reboot — leaves its
    task cgroups on disk. They hold nothing and cost nothing, but they
    accumulate and they make ``systemd-cgls`` unreadable during the next
    incident, which is when it is wanted most.

    Only ``task-*`` is touched. The daemon's own ``supervisor/`` leaf is a
    sibling of these and must survive, so the prefix match is what keeps this
    from removing the cgroup the sweeping process is sitting in.
    """
    if root is None:
        root = resolve_root(proc_root=proc_root, cgroup_root=cgroup_root)
    if root is None:
        return 0

    removed = 0
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return 0

    for name in entries:
        if not name.startswith("task-"):
            continue
        candidate = Path(root) / name
        if not candidate.is_dir():
            continue
        before = candidate.exists()
        destroy(candidate)
        if before and not candidate.exists():
            removed += 1
    return removed


def _read_text(path: Path) -> str | None:
    """Read a file, or ``None`` for every ordinary way it can be unreadable."""
    try:
        return path.read_text()
    except (OSError, ValueError):
        return None
