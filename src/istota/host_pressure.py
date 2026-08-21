"""Host memory-pressure sampling, and the breadcrumb that makes an incident attributable.

On 2026-08-20 the production host stopped serving for 41 minutes and had to be
rebooted. The kernel's own OOM dump said why: 4.64 GB of ``Shmem`` on a 7.9 GB
box with ``Total swap = 0``. tmpfs and memfd pages are swap-backed by design, so
with no swap the kernel could not evict a byte of it; its only reclaim target
was the page cache, which it took down to 1.5 MB, after which every process
faulted its own executable text back from disk continuously.

What could not be answered afterwards is *what created the 4.64 GB*. The tmpfs
cleared on reboot before it could be attributed, and no process in the dump had
it mapped — 88 tasks summed to 44 MB of ``rss_shmem`` against 4.6 GB of total.
That is the gap this module closes.

The load-bearing part is not the threshold snapshot, it is the **breadcrumb**:
one compact line at a fixed cadence, whether or not anything is wrong. The
accumulation that killed the host ran at about 35 MB/hour for five days and
never crossed a threshold until the day it became fatal, so a threshold-gated
record cannot see it. The five-day hole between the Aug 15 and Aug 20 samples is
exactly that blind spot. A leak hunt needs the flat stretches as much as the
rising ones — "it did not move between 03:00 and 09:00" is what localises an
onset to a window — which is why the cadence is fixed rather than delta-gated.

The one derived field that matters more than any raw figure is
``shmem_unaccounted = Shmem - sum(tmpfs used)``. It separates shmem that lives
in a filesystem someone can ``du`` from shmem that lives in no filesystem at all
— anonymous shared memory, overwhelmingly ``memfd_create`` regions held open by
a file descriptor. A growing tmpfs figure names a mount and a writer; a growing
residue means no mount will ever show it and the search moves to ``/proc/*/fd``.
Without the field the two cases look identical, and in August they could not be
told apart.

Design constraints, all of them consequences of *when* this code runs:

- **Standard library only, no framework imports.** A leaf module.
- **No subprocess, ever.** Not ``df``, not ``ps``, not ``docker stats``. This
  runs while the host is thrashing, and ``fork`` on a memory-starved box can
  itself trip the OOM killer. Even the container figures are gathered by
  reading files, via ``/proc/<pid>/root``, rather than by exec'ing into
  anything.
- **Nothing raises.** Every reader catches ``OSError`` and ``ValueError`` at its
  own boundary and degrades to a missing field. ``read_sample`` returns ``None``
  where ``/proc`` does not exist at all, which is every macOS dev machine and
  every kernel built without ``CONFIG_PSI``. A caller treats ``None`` as "no
  information", never as "bad".
- **``proc_root`` is a parameter on every reader**, so tests point at a fixture
  tree. No global state and no caching.

One breadcrumb costs six small file reads (``meminfo``, the three
``pressure/*`` files, ``loadavg``, ``self/mounts``) plus one ``statvfs`` and
one ``stat`` per tmpfs mount. ``snapshot`` costs a great deal more — it walks
the process table, queries Docker, and where the residue is large walks
``/proc/*/fd`` as well — so it is for a threshold crossing, never for the
interval.

Residual risk worth knowing, since the breadcrumb runs on the scheduler's main
dispatch loop: the tmpfs mount count is unbounded (one ``/run/user/N`` per
login session, one host-visible shm tmpfs per Docker container), and
``statvfs`` walks the path, so it would block if a *parent* component of a
mount point sat on a degraded filesystem. Neither is a live concern here —
``statvfs`` on a tmpfs is a pure in-memory operation, and the rclone FUSE mount
is never touched because it reports as ``fuse.rclone`` and the reader filters
on ``tmpfs`` — but Stage 3 moves the sampler to a 30-second cadence, at which
point moving the emit to ``_spawn_background_check``, as its loop neighbours
already are, is the cheap insurance.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ContainerShmUsage",
    "PressureSample",
    "ProcessRss",
    "TmpfsUsage",
    "breadcrumb",
    "container_shm_for_pid",
    "count_memfd_by_process",
    "is_under_pressure",
    "read_container_shm",
    "read_process_rss",
    "read_sample",
    "read_tmpfs_usage",
    "shmem_unaccounted_kb",
    "tmpfs_accounted_kb",
    "snapshot",
]

# Residue above this sends `snapshot` to /proc/*/fd. Below it the tmpfs table
# has already named a mount, so the walk would cost milliseconds to confirm what
# is on the line above. 512 MB is well over the 85 MB baseline this host idles
# at and well under the 4.2 GB that made the walk necessary.
_MEMFD_WALK_THRESHOLD_KB = 512 * 1024


@dataclass(frozen=True)
class PressureSample:
    """One reading of host memory state. All memory figures in kB, as /proc gives them."""

    mem_total_kb: int
    mem_available_kb: int
    shmem_kb: int
    swap_total_kb: int
    swap_free_kb: int
    cached_kb: int
    # ``None`` = the kernel did not report it, which is not the same fact as
    # zero. Debian compiles PSI in but leaves it runtime-disabled unless
    # ``psi=1`` is on the kernel cmdline, so a host with no ``/proc/pressure``
    # at all is ordinary rather than exotic — and a series where "PSI was never
    # available" is indistinguishable from "the box was idle" answers the wrong
    # question months later.
    psi_mem_some_avg10: float | None
    psi_mem_full_avg10: float | None
    psi_io_some_avg10: float | None
    psi_cpu_some_avg10: float | None
    load1: float | None
    # False = ``/proc/meminfo`` carried no ``MemAvailable`` line, so
    # ``mem_available_kb`` is a default and not a measurement. A separate flag
    # rather than ``int | None`` because the kernel *can* legitimately report
    # zero — ``si_mem_available()`` clamps a negative estimate to 0 — so the
    # value alone cannot distinguish "genuinely nothing left" from "the field
    # was not there", and those two demand opposite responses from the gate.
    # Trimmed meminfo is real: lxcfs and some container runtimes mask it.
    # Trailing with a default so every existing constructor keeps working.
    mem_available_measured: bool = True


@dataclass(frozen=True)
class TmpfsUsage:
    """A tmpfs mount as the host sees it. Bytes, because statvfs deals in blocks."""

    mount_point: str
    size_bytes: int
    used_bytes: int
    # st_dev of the mount point, so two paths onto one tmpfs are counted once
    # in the residue arithmetic. -1 = not determined (a hand-built row, or a
    # stat that failed); such rows are never deduped against anything.
    device_id: int = -1


@dataclass(frozen=True)
class ProcessRss:
    pid: int
    comm: str
    rss_kb: int
    rss_shmem_kb: int


@dataclass(frozen=True)
class ContainerShmUsage:
    """A container's shm/tmpfs mount, or an explicit record that it could not be read.

    ``available=False`` rows are emitted rather than dropped. An absent line and
    a zero line must not look alike when the whole point is attribution.
    """

    name: str
    mount_point: str
    size_bytes: int
    used_bytes: int
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class MemoryEvents:
    """cgroup v2 ``memory.events`` counters for the daemon's own cgroup.

    Monotonic counts since the cgroup was created, not rates. ``high`` is the
    one Stage 2 left unreadable: ``MemoryHigh=`` does not kill a cgroup that
    exceeds it, it applies an allocation-time sleep penalty to every process
    inside — the dispatch loop and the pollers included. So a throttled daemon
    presents as "everything slow, nothing logged", which is the same shape as
    the hang this module exists to explain. ``oom_kill`` is the counterpart for
    a hard limit and is what Stage 5's per-task cgroups will trip.
    """

    low: int = 0
    high: int = 0
    max: int = 0
    oom: int = 0
    oom_kill: int = 0
    # Which cgroup these counters actually came from. Recorded because the
    # reader walks up to find them, so the answer is not always the cgroup the
    # daemon sits in — and "high=777" means completely different things
    # depending on whose limit moved.
    source: str = ""


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _parse_meminfo(text: str) -> dict[str, int]:
    """``MemTotal:  8129380 kB`` -> ``{"MemTotal": 8129380}``, skipping junk.

    A truncated final line or a non-numeric value is dropped on its own; the
    rest of the file still parses. /proc is not supposed to produce either, but
    this reads it while the kernel is under pressure and the alternative to
    skipping is losing every figure to one bad line.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        value = rest.strip().split(" ")[0]
        try:
            out[key.strip()] = int(value)
        except ValueError:
            continue
    return out


def _parse_psi(text: str) -> dict[str, float]:
    """Pull ``avg10`` off the ``some`` and ``full`` lines of a PSI file."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        for token in parts[1:]:
            name, sep, value = token.partition("=")
            if not sep or name != "avg10":
                continue
            try:
                out[parts[0]] = float(value)
            except ValueError:
                pass
    return out


def _parse_load1(text: str) -> float | None:
    """First field of ``/proc/loadavg``, or ``None`` if it did not parse.

    ``None`` rather than 0.0 for the same reason the PSI fields use it: a load
    average of exactly zero is a real and reportable state, so a parse failure
    must not be able to impersonate one.
    """
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def read_sample(proc_root: Path = Path("/proc")) -> PressureSample | None:
    """One cheap reading of host memory state, or ``None`` where /proc has none.

    **``/proc/meminfo`` is the gate, not ``/proc/pressure/memory``.** The fields
    this module exists to record — ``Shmem``, ``SwapTotal``, ``MemAvailable`` —
    all live in meminfo and are readable with no PSI at all. Sinking the whole
    sample because an optional pressure file is missing would throw the
    load-bearing figures away to protect a supporting one, and it would do so
    on a large class of ordinary hosts: Debian compiles PSI in but ships
    ``CONFIG_PSI_DEFAULT_DISABLED=y``, so ``/proc/pressure/`` does not exist
    unless ``psi=1`` is on the kernel cmdline. A deployment in that state would
    have produced no series at all, which is precisely the outcome Stage 1
    exists to prevent. Every PSI field degrades to ``None`` instead.

    ``None`` from this function therefore means "no ``/proc/meminfo`` here" —
    macOS, or a tree that is not a Linux procfs. It also covers a meminfo that
    parsed to nothing usable (no ``MemTotal``), because a sample reading
    ``mem_total_kb=0`` is physically impossible and would enter the series as a
    measurement rather than as a gap. Never an error: a caller treats ``None``
    as no information, and the inverse reading would let a missing file halt a
    daemon.
    """
    proc_root = Path(proc_root)

    meminfo_text = _read_text(proc_root / "meminfo")
    if meminfo_text is None:
        return None

    mem = _parse_meminfo(meminfo_text)
    if "MemTotal" not in mem:
        return None

    mem_pressure_text = _read_text(proc_root / "pressure" / "memory")
    io_text = _read_text(proc_root / "pressure" / "io")
    cpu_text = _read_text(proc_root / "pressure" / "cpu")
    psi_mem = _parse_psi(mem_pressure_text) if mem_pressure_text is not None else {}
    psi_io = _parse_psi(io_text) if io_text is not None else {}
    psi_cpu = _parse_psi(cpu_text) if cpu_text is not None else {}

    loadavg_text = _read_text(proc_root / "loadavg")

    return PressureSample(
        mem_total_kb=mem.get("MemTotal", 0),
        mem_available_kb=mem.get("MemAvailable", 0),
        shmem_kb=mem.get("Shmem", 0),
        swap_total_kb=mem.get("SwapTotal", 0),
        swap_free_kb=mem.get("SwapFree", 0),
        cached_kb=mem.get("Cached", 0),
        psi_mem_some_avg10=psi_mem.get("some"),
        psi_mem_full_avg10=psi_mem.get("full"),
        psi_io_some_avg10=psi_io.get("some"),
        psi_cpu_some_avg10=psi_cpu.get("some"),
        load1=_parse_load1(loadavg_text) if loadavg_text is not None else None,
        mem_available_measured="MemAvailable" in mem,
    )


def is_under_pressure(
    sample: PressureSample, *, psi_threshold: float, min_available_mb: int
) -> bool:
    """Either signal alone is enough.

    PSI catches the acute stall — the machine spending its wall time waiting on
    memory rather than working — and ``MemAvailable`` catches the squeeze that
    has not yet turned into stalling. On 2026-08-20 both fired; a slow
    accumulation trips the second one first.

    An unmeasured PSI figure abstains rather than counting as zero, leaving the
    ``MemAvailable`` floor to decide on its own. Treating "PSI is switched off
    on this kernel" as "the machine is not stalling" would report calm from a
    host that has no way to say otherwise.

    The floor abstains on the same rule when ``MemAvailable`` was absent from
    meminfo. Reading the default zero as a measurement would put the gate below
    any floor forever, halting dispatch for the life of the process on a host
    whose only fault is a trimmed ``/proc/meminfo`` — the opposite of the
    fail-open rule this gate is built on. A threshold of zero disables its own
    arm; with both at zero the function always returns ``False``.
    """
    if (
        psi_threshold > 0
        and sample.psi_mem_some_avg10 is not None
        and sample.psi_mem_some_avg10 > psi_threshold
    ):
        return True
    if min_available_mb > 0 and sample.mem_available_measured:
        return sample.mem_available_kb < min_available_mb * 1024
    return False


def snapshot_trigger(
    sample: PressureSample,
    tmpfs: Sequence[TmpfsUsage],
    *,
    psi_threshold: float,
    min_available_mb: int,
    shmem_unaccounted_mb: int,
) -> str | None:
    """Why a snapshot should be written now, or ``None`` to stay quiet.

    Deliberately *not* the same predicate as :func:`is_under_pressure`, and the
    difference is the point. That one gates admission: it answers "is there room
    to start more work", so it reads the two figures that bear on that question
    and nothing else. This one gates attribution: it answers "is something
    happening that we will want the evidence for", which is a wider question and
    catches an event the first one is right to ignore.

    The third trigger comes from the production series rather than from theory.
    Over the 24 hours after the breadcrumb was deployed, the production host
    recorded exactly one event worth a snapshot: shmem went from 85 MB to
    1.52 GB in under five minutes, none of it in any host tmpfs mount. zram
    absorbed it — 2.27 GB of swap in use, ``MemAvailable`` never below 2.9 GB,
    ``memory some avg10`` peaking at **0.07**. Both thresholds the spec named
    would have looked straight past it, so the snapshot as originally specified
    could never have fired on the one thing it exists to identify.

    Growth in the residue is therefore its own signal. It says a large shmem
    allocation exists that no mount can account for, which is the case where
    walking ``/proc/*/fd`` is the only way to find a holder — and by the time
    such an accumulation depresses ``MemAvailable``, the evidence naming its
    owner is days old. Note what this does *not* do: a residue this size is not
    a reason to refuse work, which is why it fires the snapshot and leaves
    :func:`is_under_pressure` alone. Confusing the two would have closed the
    admission gate through a burst that zram handled perfectly well.

    Zero disables an arm, uniformly across all three: ``psi_threshold=0``,
    ``min_available_mb=0`` and ``shmem_unaccounted_mb=0`` each switch off their
    own test and leave the others standing. Without the explicit guard the PSI
    arm would invert — a bare ``> 0`` fires on almost every sample a live host
    produces, so the switch that reads as "off" would be the noisiest setting
    available. An unmeasured PSI figure abstains rather than counting as zero.

    The return is a short human-readable reason naming the figure and the
    threshold it crossed, so the log line and the admin notification say *why*
    they fired rather than leaving the reader to infer it from the snapshot.
    """
    if (
        psi_threshold > 0
        and sample.psi_mem_some_avg10 is not None
        and sample.psi_mem_some_avg10 > psi_threshold
    ):
        return (
            f"psi_mem_some_avg10={sample.psi_mem_some_avg10:.2f}>{psi_threshold:g}"
        )

    available_mb = sample.mem_available_kb // 1024
    if (
        min_available_mb > 0
        and sample.mem_available_measured
        and available_mb < min_available_mb
    ):
        return f"mem_available_mb={available_mb}<{min_available_mb}"

    if shmem_unaccounted_mb > 0:
        residue_mb = shmem_unaccounted_kb(sample, tmpfs) // 1024
        if residue_mb >= shmem_unaccounted_mb:
            return f"shmem_unaccounted_mb={residue_mb}>={shmem_unaccounted_mb}"

    return None


def read_memory_events(
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> MemoryEvents | None:
    """The daemon's own cgroup ``memory.events``, or ``None`` where there is none.

    Resolves the calling process's unified-hierarchy cgroup from
    ``/proc/self/cgroup`` rather than taking a path, so it reports on whichever
    cgroup systemd actually placed the daemon in — which, after Stage 2's
    ``DelegateSubgroup=supervisor``, is a leaf whose name the daemon does not
    otherwise know.

    **Then walks up to the nearest ancestor that has the file, which is the
    whole trick.** A cgroup only gets a controller's interface files if its
    *parent* lists that controller in ``cgroup.subtree_control``, so the leaf
    the daemon actually sits in has none: on the production host,
    ``system.slice/<unit>.service/`` carries ``memory.events`` and
    ``memory.high`` while ``…/supervisor/`` carries neither, because the unit's
    ``subtree_control`` is empty. Reading only the exact path from
    ``/proc/self/cgroup`` therefore finds nothing on the one deployment this
    was written for, and would have rendered ``?`` forever while looking like
    an honest "not available here". Walking up is also the semantically right
    answer: ``MemoryHigh=`` is applied to the *unit* cgroup, so the unit's
    counter is the one that moves.

    ``None`` covers every ordinary way this can be absent: cgroup v1, a host
    with no ``memory`` controller anywhere above the process, a container that
    does not expose the tree, and macOS. All are "no information", none are
    errors — a counter this module cannot read must not stop the breadcrumb
    that carries it. Both roots are parameters for the same reason every other
    reader here takes one: so a fixture tree scopes the whole read.
    """
    try:
        text = _read_text(Path(proc_root) / "self" / "cgroup")
        if text is None:
            return None

        # cgroup v2 puts the process on a single ``0::<path>`` line. A v1 line
        # (``11:memory:/…``) names a controller-specific hierarchy that has no
        # memory.events at all, so matching loosely here would build a path
        # that either does not exist or, worse, exists and means something else.
        rel = None
        for line in text.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
                rel = fields[2].strip()
                break
        if rel is None:
            return None

        # Walk from the process's own cgroup up to the root, stopping at the
        # first ancestor that actually has the file. Bounded by the depth of
        # the path, and it never escapes ``cgroup_root``.
        base = Path(cgroup_root)
        # ``..`` is dropped rather than trusted. A real procfs cannot produce
        # one, but this reader takes its roots as parameters so a fixture (or a
        # container's rewritten cgroup line) can, and joining it would walk out
        # of ``cgroup_root`` — which the loop below otherwise assumes it cannot.
        parts = [p for p in rel.split("/") if p and p != ".."]

        # Do not walk above our own unit. Past that point the counters belong
        # to `system.slice` or to the cgroup root, which aggregate every
        # service on the box — and `memory_events_high=777` read off the slice
        # says "something on this host was throttled", not "istota hit its own
        # MemoryHigh". That inverts the one diagnostic this field exists for,
        # so a miss is reported as a miss instead.
        floor = 0
        for i, part in enumerate(parts):
            if part.endswith((".service", ".scope")):
                floor = i + 1
                break

        # **Prefer the unit cgroup outright rather than the nearest ancestor
        # that happens to have the file.** Those were the same answer until
        # per-task cgroups shipped, and the difference is invisible off-host.
        #
        # `memory.events` appears in a cgroup only when its *parent* enables
        # the memory controller in `cgroup.subtree_control`. That file was
        # empty here, so the `supervisor/` leaf had no counters and the walk
        # landed on the unit. `task_cgroup.enable_controllers` now writes
        # `+memory` to the unit's `subtree_control` — it must, or a task cgroup
        # would have no `memory.max` — which gives the leaf its own
        # `memory.events` and stops the walk one level early.
        #
        # That silently kills both readings. `MemoryHigh=` is set on the
        # *unit*, so the leaf's `memory.high` is `max` and its `high` counter
        # can never move — the A3 throttle diagnostic this field exists for
        # reads a permanent zero. And `memory.events` is hierarchical, so the
        # unit's counter aggregates its descendants while the leaf's covers
        # only its own subtree; `task-<id>` cgroups are *siblings* of the leaf,
        # so a task OOM-killed inside its own cgroup — the event Stage 5 exists
        # to cause — would not be counted at all.
        #
        # Observed on the live host, not derived: leaf `memory.high=max`
        # against unit `memory.high=5368709120`.
        candidates = []
        if floor:
            candidates.append(parts[:floor])
        # Fall back to the old leaf-upward walk, which the floor still stops at
        # the unit. Only reached where the unit carries no counters at all.
        probe = parts[:]
        while True:
            if probe != parts[:floor]:
                candidates.append(probe[:])
            if len(probe) <= floor:
                break
            probe.pop()

        events_text = None
        resolved = None
        for candidate_parts in candidates:
            events_text = _read_text(
                base.joinpath(*candidate_parts) / "memory.events"
            )
            if events_text is not None:
                resolved = candidate_parts
                break
        if events_text is None or resolved is None:
            return None
        source = "/" + "/".join(resolved)

        values: dict[str, int] = {}
        for line in events_text.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                values[fields[0]] = int(fields[1])
            except ValueError:
                # A field that will not parse is left at its default rather
                # than sinking the other four. Same rule as _parse_meminfo.
                continue

        return MemoryEvents(
            low=values.get("low", 0),
            high=values.get("high", 0),
            max=values.get("max", 0),
            oom=values.get("oom", 0),
            oom_kill=values.get("oom_kill", 0),
            source=source,
        )
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# tmpfs
# ---------------------------------------------------------------------------

# /proc/self/mounts escapes these four characters in octal. Backslash is last
# on the way in and first on the way out, so the two are exact inverses: a
# mount literally named ``\040`` arrives as ``\134040`` and must not be
# unescaped to a space.
_MOUNT_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))
# Comma and colon are *not* escaped by the kernel and are legal in a mount
# point, but the breadcrumb delimits its tmpfs list with them, so the renderer
# has to escape them itself. ``=`` likewise, since every field is ``key=value``.
_FIELD_EXTRA_ESCAPES = ((",", "\\054"), (":", "\\072"), ("=", "\\075"))


def _unescape_mount(raw: str) -> str:
    out = raw
    for escaped, plain in _MOUNT_ESCAPES:
        out = out.replace(escaped, plain)
    return out


def _escape_field(value: str) -> str:
    """Make an arbitrary string safe to drop into a ``key=value`` line.

    The breadcrumb and the snapshot both promise a parseable format, and both
    render strings the *kernel* chose: a mount point, a process ``comm``, a
    container name. A mount point may contain a space (the kernel escapes it in
    the mount table and ``read_tmpfs_usage`` deliberately unescapes it again for
    ``statvfs``), and a ``comm`` may contain one outright — Firefox's content
    processes are called ``Web Content``. Either lands a bare space in the
    middle of a field and silently breaks the split for every reader
    downstream, which is worse than an ugly value because the line still looks
    well-formed.
    """
    out = value.replace("\\", "\\134")
    for plain, escaped in ((" ", "\\040"), ("\t", "\\011"), ("\n", "\\012")):
        out = out.replace(plain, escaped)
    for plain, escaped in _FIELD_EXTRA_ESCAPES:
        out = out.replace(plain, escaped)
    return out


def _usage_from_statvfs(st) -> tuple[int, int]:
    """``(size_bytes, used_bytes)`` from a statvfs result."""
    frsize = st.f_frsize or st.f_bsize
    size = st.f_blocks * frsize
    used = (st.f_blocks - st.f_bfree) * frsize
    return size, max(0, used)


def read_tmpfs_usage(
    mounts_path: Path = Path("/proc/self/mounts"),
    *,
    statvfs: Callable[[str], object] = os.statvfs,
    stat: Callable[[str], object] = os.stat,
) -> list[TmpfsUsage]:
    """Every tmpfs mount with its size and used bytes, in mount order.

    ``statvfs`` and ``stat`` are injectable so tests can describe a mount table
    without needing the mounts to exist. A mount that cannot be statted is
    dropped rather than reported as zero — it was almost certainly unmounted
    between reading the table and asking about it, and a zero would be a lie
    the ``shmem_unaccounted`` arithmetic would then inherit.
    """
    text = _read_text(Path(mounts_path))
    if text is None:
        return []

    out: list[TmpfsUsage] = []
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) < 3 or parts[2] != "tmpfs":
            continue
        mount_point = _unescape_mount(parts[1])
        try:
            size, used = _usage_from_statvfs(statvfs(mount_point))
        except (OSError, ValueError, AttributeError):
            continue
        # Filesystem identity, so `shmem_unaccounted_kb` can tell two paths onto
        # one tmpfs from two tmpfs filesystems. Best-effort: a stat that fails
        # leaves -1, which simply opts that row out of deduplication.
        try:
            device_id = stat(mount_point).st_dev
        except (OSError, ValueError, AttributeError):
            device_id = -1
        out.append(
            TmpfsUsage(
                mount_point=mount_point,
                size_bytes=size,
                used_bytes=used,
                device_id=device_id,
            )
        )
    return out


def tmpfs_accounted_kb(tmpfs: Sequence[TmpfsUsage]) -> int:
    """Total tmpfs usage in kB, counting each filesystem once.

    **Deduplication is not tidiness, it is the difference between a signal and
    a false negative.** ``statvfs`` on a bind mount reports the whole underlying
    filesystem, so a tmpfs reachable at two paths contributes its usage twice;
    with the floor in ``shmem_unaccounted_kb`` below, a large enough overcount
    renders as ``shmem_unaccounted_kb=0``, which reads as "every byte of shmem
    lives in a mount someone can ``du``" — the exact conclusion that would call
    off the search. Rows whose device could not be determined (``device_id ==
    -1``) are each counted separately, since nothing says they are duplicates.

    Truncated per mount rather than summed in bytes and truncated once, so the
    figure agrees with the per-mount list the breadcrumb prints beside it. A
    reader who recomputes the residue from the rendered fields must get the
    rendered residue back.
    """
    total_kb = 0
    seen_devices: set[int] = set()
    for mount in tmpfs:
        if mount.device_id != -1:
            if mount.device_id in seen_devices:
                continue
            seen_devices.add(mount.device_id)
        total_kb += mount.used_bytes // 1024
    return total_kb


def shmem_unaccounted_kb(sample: PressureSample, tmpfs: Sequence[TmpfsUsage]) -> int:
    """``Shmem`` minus the summed tmpfs usage, floored at zero.

    The residue that separates shmem living in a filesystem from shmem living
    in none. A growing tmpfs figure names a mount and a writer; a growing
    residue means no mount will ever show it.

    The floor is not defensive tidying. The two figures are read at slightly
    different instants, and a container's shm mount can be counted by both the
    host's mount table and the kernel's ``Shmem`` total, so the subtraction can
    legitimately go negative by a small margin. A negative here would read as a
    parsing bug to whoever greps the series months later. The breadcrumb prints
    ``tmpfs_sum_kb`` alongside, so a residue floored to zero by a *large*
    overcount stays visible as an inconsistency rather than passing for a
    finding.
    """
    return max(0, sample.shmem_kb - tmpfs_accounted_kb(tmpfs))


# ---------------------------------------------------------------------------
# The breadcrumb
# ---------------------------------------------------------------------------


def _fmt_float(value: float | None) -> str:
    """``?`` for a figure the kernel never reported, two decimals otherwise."""
    return "?" if value is None else f"{value:.2f}"


def breadcrumb(
    sample: PressureSample,
    tmpfs: Sequence[TmpfsUsage],
    events: MemoryEvents | None = None,
) -> str:
    """The single line written every interval.

    **This is a data format, not log chatter.** It will be grepped and parsed
    after the fact, quite possibly by a future version of this module reading a
    series written by an older one, so the field order and the key names are
    fixed and a rename is a breaking change. Everything is ``key=value``,
    space-separated, matching the ``scheduler_stats`` and devbox-proxy audit
    lines already in the tree.

    Every memory figure is kB — the unit ``/proc/meminfo`` uses — including the
    per-mount tmpfs list, which ``TmpfsUsage`` carries in bytes. Mixing units on
    one line is how a 4.6 GB figure gets read as 4.6 MB during an outage.

    A figure the kernel did not report renders as ``?``, never as ``0.00``.
    "PSI is switched off on this kernel" and "the machine was not stalling" are
    different facts and a series that conflates them cannot be read back.

    Mount points are re-escaped on the way out (``_escape_field``). The kernel
    hands them escaped, ``read_tmpfs_usage`` unescapes them so ``statvfs`` can
    use them, and rendering that unescaped form would put a bare space inside a
    field and break the split for every downstream reader.

    The variable-length tmpfs list goes last so a parser can take the fixed
    fields positionally. It renders ``-`` rather than an empty value when there
    are no tmpfs mounts, because ``k=`` followed by a space is ambiguous to
    split on. New fixed fields are appended after the existing ones and before
    that list, which is what keeps every earlier field at the position a reader
    of the older series already learned.

    ``events`` is optional and renders ``?`` when absent, on the same rule as
    the PSI fields: a host with no delegated cgroup has not reported that
    nothing was throttled, it has reported nothing.
    """
    mounts = (
        ",".join(
            f"{_escape_field(m.mount_point)}:{m.used_bytes // 1024}" for m in tmpfs
        )
        or "-"
    )
    return " ".join(
        [
            "host_pressure",
            f"mem_total_kb={sample.mem_total_kb}",
            f"mem_available_kb={sample.mem_available_kb if sample.mem_available_measured else '?'}",
            f"shmem_kb={sample.shmem_kb}",
            f"shmem_unaccounted_kb={shmem_unaccounted_kb(sample, tmpfs)}",
            f"tmpfs_sum_kb={tmpfs_accounted_kb(tmpfs)}",
            f"swap_total_kb={sample.swap_total_kb}",
            f"swap_free_kb={sample.swap_free_kb}",
            f"cached_kb={sample.cached_kb}",
            f"psi_mem_some_avg10={_fmt_float(sample.psi_mem_some_avg10)}",
            f"psi_mem_full_avg10={_fmt_float(sample.psi_mem_full_avg10)}",
            f"psi_io_some_avg10={_fmt_float(sample.psi_io_some_avg10)}",
            f"psi_cpu_some_avg10={_fmt_float(sample.psi_cpu_some_avg10)}",
            f"load1={_fmt_float(sample.load1)}",
            f"memory_events_high={'?' if events is None else events.high}",
            f"memory_events_oom_kill={'?' if events is None else events.oom_kill}",
            f"memory_events_cgroup={'?' if events is None else _escape_field(events.source)}",
            f"tmpfs_used_kb={mounts}",
        ]
    )


# ---------------------------------------------------------------------------
# Process table
# ---------------------------------------------------------------------------


def _pid_dirs(proc_root: Path) -> list[Path]:
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    return [proc_root / e for e in entries if e.isdigit()]


def _read_process(pid_dir: Path) -> ProcessRss | None:
    """Parse one ``/proc/<pid>/status``, or ``None`` if it went away.

    Kernel threads have no ``VmRSS:`` line and are omitted: a table of them
    crowds out the processes that actually hold memory.
    """
    text = _read_text(pid_dir / "status")
    if text is None:
        return None

    comm = "?"
    rss_kb: int | None = None
    rss_shmem_kb = 0
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        value = rest.strip()
        if key == "Name":
            comm = value or "?"
        elif key == "VmRSS":
            try:
                rss_kb = int(value.split(" ")[0])
            except (ValueError, IndexError):
                pass
        elif key == "RssShmem":
            try:
                rss_shmem_kb = int(value.split(" ")[0])
            except (ValueError, IndexError):
                pass

    if rss_kb is None:
        return None
    try:
        pid = int(pid_dir.name)
    except ValueError:
        return None
    return ProcessRss(pid=pid, comm=comm, rss_kb=rss_kb, rss_shmem_kb=rss_shmem_kb)


def read_process_rss(
    proc_root: Path = Path("/proc"), top_n: int = 20
) -> list[ProcessRss]:
    """The top ``top_n`` processes by RSS, descending.

    ``rss_shmem_kb`` comes along because its *absence* was the 2026-08-20
    incident's loudest signal: 88 tasks summed to 44 MB of mapped shmem against
    4.6 GB of total, which is what said the memory was held unmapped and sent
    the search to ``/proc/*/fd``.
    """
    rows = [p for d in _pid_dirs(Path(proc_root)) if (p := _read_process(d)) is not None]
    rows.sort(key=lambda r: r.rss_kb, reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# memfd attribution
# ---------------------------------------------------------------------------


def _tmpfs_prefixes(proc_root: Path) -> tuple[str, ...]:
    text = _read_text(proc_root / "self" / "mounts")
    if text is None:
        return ()
    prefixes = []
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) >= 3 and parts[2] == "tmpfs":
            prefixes.append(_unescape_mount(parts[1]))
    return tuple(prefixes)


def _is_unlinked_shmem(target: str, tmpfs_prefixes: Sequence[str]) -> bool:
    """Does this fd symlink target point at shmem that no mount will show?

    Two shapes. ``/memfd:name (deleted)`` is an anonymous region from
    ``memfd_create`` — it was never in a filesystem. A ``(deleted)`` path under
    a tmpfs mount was, but has been unlinked, so it no longer shows in a ``du``
    of that mount while its pages stay resident until the last fd closes. Both
    are invisible to every other figure in the snapshot.
    """
    if target.startswith("/memfd:") or target.startswith("memfd:"):
        return True
    if not target.endswith(" (deleted)"):
        return False
    path = target[: -len(" (deleted)")]
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in tmpfs_prefixes
    )


def count_memfd_by_process(proc_root: Path = Path("/proc")) -> list[tuple[int, str, int]]:
    """``(pid, comm, count)`` for every process holding unlinked shmem fds, descending.

    The expensive part of this module — a readlink per open descriptor across
    every process — so ``snapshot`` only calls it when ``shmem_unaccounted`` is
    large enough that nothing else can point at a culprit. Never called from the
    breadcrumb.

    A process whose ``fd/`` cannot be listed (it exited, or it belongs to
    another user and the daemon is unprivileged) is skipped silently. Partial
    attribution is the normal case and is still worth having.
    """
    proc_root = Path(proc_root)
    prefixes = _tmpfs_prefixes(proc_root)

    out: list[tuple[int, str, int]] = []
    for pid_dir in _pid_dirs(proc_root):
        fd_dir = pid_dir / "fd"
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            continue

        count = 0
        for entry in entries:
            try:
                target = os.readlink(fd_dir / entry)
            except OSError:
                continue
            if _is_unlinked_shmem(target, prefixes):
                count += 1
        if count == 0:
            continue

        try:
            pid = int(pid_dir.name)
        except ValueError:
            continue
        proc = _read_process(pid_dir)
        out.append((pid, proc.comm if proc is not None else "?", count))

    out.sort(key=lambda row: row[2], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Container shm
# ---------------------------------------------------------------------------

# The host's own mount table does not show what a container mounted inside its
# namespace, so a container's /dev/shm — 2 GB of allowance for the browser — is
# a blind spot from up here. The way across is not `docker exec`: the kernel
# resolves /proc/<pid>/root against that process's mount namespace, so a plain
# statvfs on /proc/<container-init-pid>/root/dev/shm reads the container's tmpfs
# with no fork on either side of the boundary. Docker is asked one thing only,
# over a read-only GET: which pid.
_DOCKER_TIMEOUT_SECONDS = 2.0


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket. Docker's API speaks plain HTTP there."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D102
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _docker_get(socket_path: Path, path: str, timeout: float):
    """One GET against the Docker API. Raises on anything unexpected."""
    conn = _UnixHTTPConnection(str(socket_path), timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost", "Accept": "application/json"})
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"docker api {path} returned {response.status}")
        return json.loads(body)
    finally:
        conn.close()


def container_shm_for_pid(
    name: str,
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    statvfs: Callable[[str], object] = os.statvfs,
) -> list[ContainerShmUsage]:
    """Every tmpfs mount inside one container, read through ``/proc/<pid>/root``.

    A pid of 0 means Docker reports the container as not running. That is
    recorded as an unavailable row rather than dropped — "the browser container
    was stopped" and "the browser container held nothing" are different facts,
    and only one of them clears it as a suspect.
    """
    proc_root = Path(proc_root)
    if pid <= 0:
        return [
            ContainerShmUsage(name, "?", 0, 0, available=False, detail="container not running")
        ]

    mounts_text = _read_text(proc_root / str(pid) / "mounts")
    if mounts_text is None:
        return [
            ContainerShmUsage(
                name,
                "?",
                0,
                0,
                available=False,
                detail=f"/proc/{pid}/mounts unreadable",
            )
        ]

    out: list[ContainerShmUsage] = []
    for line in mounts_text.splitlines():
        parts = line.split(" ")
        if len(parts) < 3 or parts[2] != "tmpfs":
            continue
        mount_point = _unescape_mount(parts[1])
        host_path = proc_root / str(pid) / "root" / mount_point.lstrip("/")
        try:
            size, used = _usage_from_statvfs(statvfs(str(host_path)))
        except (OSError, ValueError, AttributeError) as exc:
            out.append(
                ContainerShmUsage(
                    name, mount_point, 0, 0, available=False, detail=f"statvfs failed: {exc}"
                )
            )
            continue
        out.append(ContainerShmUsage(name, mount_point, size, used, available=True))

    if not out:
        out.append(
            ContainerShmUsage(name, "?", 0, 0, available=False, detail="no tmpfs mounts")
        )
    return out


def read_container_shm(
    docker_socket: Path = Path("/var/run/docker.sock"),
    *,
    proc_root: Path = Path("/proc"),
    timeout: float = _DOCKER_TIMEOUT_SECONDS,
) -> list[ContainerShmUsage]:
    """tmpfs usage inside every running container.

    The container set comes from Docker's own running list rather than from the
    compose files. Compose names one of the two containers that matter
    statically (``istota-browser``) and interpolates the other
    (``devbox-${USER_NAME}``), so a static list would miss every devbox on the
    box — and would go stale the moment a service is added.

    Docker being unreachable yields a single row named ``?`` rather than an
    empty list. An empty list means "no container holds tmpfs", which is a
    finding; unreachable means the question was never asked, which is not.

    ``docker_socket`` defaults to the real socket rather than to
    ``docker_proxy``'s per-user allowlist socket, and that is deliberate in two
    ways. The proxy is scoped to one user's own container, while this needs the
    host-wide list; and the daemon only reaches the real socket at all if the
    operator put its user in the ``docker`` group, so an unprivileged
    deployment degrades to the "unreachable" row instead of gaining a
    capability it did not have. The handle is nonetheless root-equivalent, so
    the path stays a *parameter* — when Stage 3 wires ``snapshot`` it should
    supply it from config rather than let this default stand by accident. Every
    request this module makes is a GET (``_docker_get`` hard-codes the method).
    """
    docker_socket = Path(docker_socket)
    try:
        containers = _docker_get(docker_socket, "/containers/json", timeout)
    except Exception as exc:  # noqa: BLE001  -- socket, HTTP, JSON, all the same answer here
        return [
            ContainerShmUsage(
                "?", "?", 0, 0, available=False, detail=f"docker api unreachable: {exc}"
            )
        ]

    out: list[ContainerShmUsage] = []
    for entry in containers if isinstance(containers, list) else []:
        names = entry.get("Names") or []
        name = names[0].lstrip("/") if names else str(entry.get("Id", "?"))[:12]
        try:
            detail = _docker_get(docker_socket, f"/containers/{entry['Id']}/json", timeout)
            pid = int(detail.get("State", {}).get("Pid", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            out.append(
                ContainerShmUsage(
                    name, "?", 0, 0, available=False, detail=f"inspect failed: {exc}"
                )
            )
            continue
        out.extend(container_shm_for_pid(name, pid, proc_root=proc_root))
    return out


# ---------------------------------------------------------------------------
# The threshold snapshot
# ---------------------------------------------------------------------------


def snapshot(
    proc_root: Path = Path("/proc"),
    top_n: int = 20,
    *,
    sample: PressureSample | None = None,
    tmpfs: Sequence[TmpfsUsage] | None = None,
    containers: Sequence[ContainerShmUsage] | None = None,
    statvfs: Callable[[str], object] = os.statvfs,
    docker_socket: Path = Path("/var/run/docker.sock"),
) -> str:
    """The multi-line block written on a threshold crossing.

    Answers the question the incident could not: which allocation holds the
    memory. Reads the headline figures, every tmpfs mount, every container's
    tmpfs mounts, the top processes by RSS, and — only when the residue says no
    mount can account for it — the per-process count of unlinked shmem
    descriptors.

    Best-effort throughout. Whatever could not be read is named on its own line
    rather than omitted, because during an outage a missing line and a zero line
    are indistinguishable and only one of them is evidence.

    ``tmpfs`` and ``containers`` are injectable so a caller that already sampled
    them (and a test) need not pay for the reads twice. ``statvfs`` and
    ``docker_socket`` exist so that pointing ``proc_root`` at a fixture tree
    scopes the *whole* snapshot: without them a fixture run would still stat
    real host paths and still open the live Docker socket, which would make
    ``--proc-root`` a half-truth.
    """
    proc_root = Path(proc_root)
    lines = ["host_pressure_snapshot"]

    # Prefer the caller's sample. Re-reading here would let the block's own
    # headline figures disagree with the `trigger=` line printed beside them —
    # seconds pass while the Docker round-trips run, and a block reading
    # `mem_available_kb=3100000` under `trigger=mem_available_mb=700<768` reads
    # as a bug in the trigger rather than as a fast-moving host.
    if sample is None:
        sample = read_sample(proc_root)
    if sample is None:
        lines.append("  sample=unavailable (no readable /proc/meminfo)")
    else:
        lines.append(
            "  " + " ".join(
                [
                    f"mem_total_kb={sample.mem_total_kb}",
                    f"mem_available_kb={sample.mem_available_kb}",
                    f"shmem_kb={sample.shmem_kb}",
                    f"swap_total_kb={sample.swap_total_kb}",
                    f"swap_free_kb={sample.swap_free_kb}",
                    f"cached_kb={sample.cached_kb}",
                ]
            )
        )
        lines.append(
            "  " + " ".join(
                [
                    f"psi_mem_some_avg10={_fmt_float(sample.psi_mem_some_avg10)}",
                    f"psi_mem_full_avg10={_fmt_float(sample.psi_mem_full_avg10)}",
                    f"psi_io_some_avg10={_fmt_float(sample.psi_io_some_avg10)}",
                    f"psi_cpu_some_avg10={_fmt_float(sample.psi_cpu_some_avg10)}",
                    f"load1={_fmt_float(sample.load1)}",
                ]
            )
        )

    if tmpfs is None:
        tmpfs = read_tmpfs_usage(proc_root / "self" / "mounts", statvfs=statvfs)
    if not tmpfs:
        lines.append("  tmpfs none-readable")
    for mount in tmpfs:
        lines.append(
            f"  tmpfs mount={_escape_field(mount.mount_point)} "
            f"size_kb={mount.size_bytes // 1024} used_kb={mount.used_bytes // 1024}"
        )
    lines.append(f"  tmpfs_sum_kb={tmpfs_accounted_kb(tmpfs)}")

    # No sample means no Shmem to subtract from, so there is no residue — and
    # printing 0 would say "nothing is unaccounted for", which is a finding
    # rather than the absence of one.
    if sample is None:
        residue_kb = 0
        lines.append("  shmem_unaccounted_kb=unavailable")
    else:
        residue_kb = shmem_unaccounted_kb(sample, tmpfs)
        lines.append(f"  shmem_unaccounted_kb={residue_kb}")

    if containers is None:
        containers = read_container_shm(docker_socket, proc_root=proc_root)
    for container in containers:
        if container.available:
            lines.append(
                f"  container name={_escape_field(container.name)} "
                f"mount={_escape_field(container.mount_point)} "
                f"used_kb={container.used_bytes // 1024} "
                f"size_kb={container.size_bytes // 1024}"
            )
        else:
            lines.append(
                f"  container name={_escape_field(container.name)} "
                f"mount={_escape_field(container.mount_point)} "
                f"used_kb=unavailable detail={container.detail}"
            )

    processes = read_process_rss(proc_root, top_n=top_n)
    lines.append(f"  processes_read={len(processes)}")
    for proc in processes:
        lines.append(
            f"  proc pid={proc.pid} comm={_escape_field(proc.comm)} "
            f"rss_kb={proc.rss_kb} rss_shmem_kb={proc.rss_shmem_kb}"
        )

    if residue_kb >= _MEMFD_WALK_THRESHOLD_KB:
        memfd = count_memfd_by_process(proc_root)
        if not memfd:
            lines.append("  memfd none-found")
        for pid, comm, count in memfd:
            lines.append(f"  memfd pid={pid} comm={_escape_field(comm)} count={count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# `python -m istota.host_pressure`
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Print one live breadcrumb, or the full snapshot with ``--snapshot``.

    The way an operator checks on a host that the module reads it correctly,
    without waiting an interval for the daemon to log one.
    """
    import argparse  # noqa: PLC0415  -- CLI-only, not paid for on the daemon path

    parser = argparse.ArgumentParser(
        prog="python -m istota.host_pressure",
        description="Print one host-pressure breadcrumb line, or a full snapshot.",
    )
    parser.add_argument("--snapshot", action="store_true", help="print the full snapshot instead")
    parser.add_argument("--proc-root", default="/proc", help="read from this tree instead of /proc")
    parser.add_argument("--top", type=int, default=20, help="processes in the snapshot table")
    parser.add_argument(
        "--docker-socket",
        default="/var/run/docker.sock",
        help="Docker API socket the snapshot asks for container pids (unreachable is fine)",
    )
    args = parser.parse_args(argv)

    proc_root = Path(args.proc_root)

    if args.snapshot:
        print(snapshot(proc_root, top_n=args.top, docker_socket=Path(args.docker_socket)))
        return 0

    sample = read_sample(proc_root)
    if sample is None:
        print(
            f"host_pressure unavailable: no readable {proc_root}/meminfo "
            f"(not Linux, or not a procfs)"
        )
        return 1
    print(breadcrumb(sample, read_tmpfs_usage(proc_root / "self" / "mounts")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
