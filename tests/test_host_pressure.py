"""Tests for ``istota.host_pressure``.

Every reader in that module takes its root as a parameter, so these tests build
a fixture ``/proc`` tree under ``tmp_path`` and point the readers at it. Nothing
here mocks ``open`` and nothing reads the real ``/proc`` — the module has to run
on the macOS dev machines too, where none of these files exist.

The numbers are the 2026-08-20 incident's own, from the ``Mem-Info`` block of
the final OOM: ``Shmem`` 4,641,344 kB against a 7.9 GB box with ``Total swap =
0`` and 1.5 MB of page cache left. They are here as a regression fixture, so a
future change to the parsing or the breadcrumb format has to keep rendering the
figures that made the outage legible.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from istota import host_pressure


# ---------------------------------------------------------------------------
# Fixture-tree helpers
# ---------------------------------------------------------------------------

# 7.9 GB box, mid-incident: 289 MB available, 4.64 GB of shmem, no swap, and a
# page cache the kernel has already squeezed down to 1.5 MB.
INCIDENT_MEMINFO = """\
MemTotal:        8129380 kB
MemFree:          128044 kB
MemAvailable:     296284 kB
Buffers:             128 kB
Cached:             1508 kB
SwapCached:            0 kB
Active:          1204812 kB
Inactive:        4698112 kB
Shmem:           4641344 kB
SwapTotal:             0 kB
SwapFree:              0 kB
Dirty:                40 kB
"""

INCIDENT_PRESSURE_MEMORY = """\
some avg10=87.20 avg60=71.44 avg300=44.03 total=1284410293
full avg10=60.00 avg60=48.12 avg300=29.87 total=982114402
"""

INCIDENT_PRESSURE_IO = """\
some avg10=39.10 avg60=35.02 avg300=22.18 total=884120391
full avg10=31.05 avg60=28.44 avg300=18.02 total=712004881
"""

INCIDENT_PRESSURE_CPU = """\
some avg10=0.00 avg60=0.11 avg300=0.40 total=44120391
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
"""

# The same host after the reboot: 4487 MB available, 82 MB of shmem, quiet PSI.
HEALTHY_MEMINFO = """\
MemTotal:        8129380 kB
MemFree:         3908112 kB
MemAvailable:    4594688 kB
Cached:          1204812 kB
Shmem:             84992 kB
SwapTotal:       4064688 kB
SwapFree:        4064688 kB
"""

HEALTHY_PRESSURE_MEMORY = """\
some avg10=0.00 avg60=0.00 avg300=0.00 total=1204
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
"""


def build_proc(
    root: Path,
    *,
    meminfo: str | None = INCIDENT_MEMINFO,
    pressure_memory: str | None = INCIDENT_PRESSURE_MEMORY,
    pressure_io: str | None = INCIDENT_PRESSURE_IO,
    pressure_cpu: str | None = INCIDENT_PRESSURE_CPU,
    loadavg: str | None = "76.12 68.40 41.03 8/812 31904\n",
    mounts: str | None = None,
) -> Path:
    """Lay down a minimal fixture ``/proc``. A ``None`` omits that file."""
    root.mkdir(parents=True, exist_ok=True)
    if meminfo is not None:
        (root / "meminfo").write_text(meminfo)
    if loadavg is not None:
        (root / "loadavg").write_text(loadavg)
    if pressure_memory is not None or pressure_io is not None or pressure_cpu is not None:
        pressure = root / "pressure"
        pressure.mkdir(exist_ok=True)
        if pressure_memory is not None:
            (pressure / "memory").write_text(pressure_memory)
        if pressure_io is not None:
            (pressure / "io").write_text(pressure_io)
        if pressure_cpu is not None:
            (pressure / "cpu").write_text(pressure_cpu)
    if mounts is not None:
        selfdir = root / "self"
        selfdir.mkdir(exist_ok=True)
        (selfdir / "mounts").write_text(mounts)
    return root


def add_process(
    root: Path,
    pid: int,
    *,
    name: str = "proc",
    rss_kb: int | None = 0,
    rss_shmem_kb: int = 0,
    fds: dict[str, str] | None = None,
    fd_unreadable: bool = False,
) -> Path:
    """Add ``<root>/<pid>`` with a ``status`` file and optional ``fd/`` links.

    ``rss_kb=None`` omits the ``VmRSS:`` line entirely (a kernel thread).
    ``fds`` maps fd number to the symlink target.
    """
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"Name:\t{name}", "State:\tS (sleeping)", f"Pid:\t{pid}"]
    if rss_kb is not None:
        lines.append(f"VmRSS:\t{rss_kb:>8} kB")
        lines.append(f"RssShmem:\t{rss_shmem_kb:>8} kB")
    (d / "status").write_text("\n".join(lines) + "\n")
    if fd_unreadable:
        # A regular file where ``fd/`` should be: os.listdir raises OSError,
        # which is the branch under test. Deterministic, and it does not depend
        # on the test process being unprivileged the way chmod 000 would.
        (d / "fd").write_text("not a directory\n")
    elif fds is not None:
        fdd = d / "fd"
        fdd.mkdir(exist_ok=True)
        for num, target in fds.items():
            os.symlink(target, fdd / num)
    return d


def fake_statvfs(table: dict[str, tuple[int, int]]):
    """Return a statvfs stand-in over ``{mount: (size_bytes, used_bytes)}``.

    Frame size is pinned at 4096 so the block arithmetic in the module is what
    is actually under test rather than the fixture's choice of units.
    """
    frsize = 4096

    class _Result:
        def __init__(self, blocks: int, bfree: int) -> None:
            self.f_frsize = frsize
            self.f_bsize = frsize
            self.f_blocks = blocks
            self.f_bfree = bfree
            self.f_bavail = bfree

    def _statvfs(path):
        key = str(path)
        if key not in table:
            raise OSError(2, "No such file or directory", key)
        size, used = table[key]
        blocks = size // frsize
        return _Result(blocks, blocks - used // frsize)

    return _statvfs


def fake_stat(devices: dict[str, int]):
    """``os.stat`` stand-in returning a chosen ``st_dev`` per mount point.

    ``read_tmpfs_usage`` records the device so the residue arithmetic can count
    one filesystem once however many paths reach it. An unlisted path raises,
    which is the "device unknown" path (``device_id == -1``).
    """

    class _Stat:
        def __init__(self, dev: int) -> None:
            self.st_dev = dev

    def _stat(path):
        key = str(path)
        if key not in devices:
            raise OSError(2, "No such file or directory", key)
        return _Stat(devices[key])

    return _stat


# ---------------------------------------------------------------------------
# read_sample
# ---------------------------------------------------------------------------


class TestReadSample:
    def test_parses_the_incident_numbers(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)

        assert sample is not None
        assert sample.mem_total_kb == 8129380
        assert sample.mem_available_kb == 296284
        assert sample.shmem_kb == 4641344
        assert sample.swap_total_kb == 0
        assert sample.swap_free_kb == 0
        assert sample.cached_kb == 1508
        assert sample.psi_mem_some_avg10 == pytest.approx(87.20)
        assert sample.psi_mem_full_avg10 == pytest.approx(60.00)
        assert sample.psi_io_some_avg10 == pytest.approx(39.10)
        assert sample.psi_cpu_some_avg10 == pytest.approx(0.00)
        assert sample.load1 == pytest.approx(76.12)

    def test_absent_psi_does_not_sink_the_memory_figures(self, tmp_path):
        """Debian compiles PSI in but leaves it runtime-disabled without
        ``psi=1``, so ``/proc/pressure`` is missing on plenty of ordinary
        hosts. Losing Shmem there would make the whole stage a silent no-op."""
        proc = build_proc(
            tmp_path / "proc",
            pressure_memory=None,
            pressure_io=None,
            pressure_cpu=None,
        )
        sample = host_pressure.read_sample(proc)

        assert sample is not None
        assert sample.shmem_kb == 4641344
        assert sample.swap_total_kb == 0
        assert sample.mem_available_kb == 296284
        # Unmeasured, which is not the same fact as zero.
        assert sample.psi_mem_some_avg10 is None
        assert sample.psi_mem_full_avg10 is None
        assert sample.psi_io_some_avg10 is None
        assert sample.psi_cpu_some_avg10 is None

    def test_returns_none_when_meminfo_absent(self, tmp_path):
        proc = build_proc(tmp_path / "proc", meminfo=None)
        assert host_pressure.read_sample(proc) is None

    def test_returns_none_when_meminfo_has_no_memtotal(self, tmp_path):
        """An all-zero sample is physically impossible and would enter the
        series as a measurement rather than as a gap."""
        proc = build_proc(tmp_path / "proc", meminfo="garbage\nmore garbage\n")
        assert host_pressure.read_sample(proc) is None

    def test_returns_none_on_an_entirely_absent_proc(self, tmp_path):
        assert host_pressure.read_sample(tmp_path / "nope") is None

    def test_missing_io_and_cpu_pressure_degrade_to_none(self, tmp_path):
        proc = build_proc(tmp_path / "proc", pressure_io=None, pressure_cpu=None)
        sample = host_pressure.read_sample(proc)

        assert sample is not None
        assert sample.psi_io_some_avg10 is None
        assert sample.psi_cpu_some_avg10 is None
        # The memory figures still parsed.
        assert sample.psi_mem_some_avg10 == pytest.approx(87.20)

    def test_a_kernel_reporting_only_some_leaves_full_unmeasured(self, tmp_path):
        proc = build_proc(
            tmp_path / "proc", pressure_memory="some avg10=12.50 avg60=1 total=1\n"
        )
        sample = host_pressure.read_sample(proc)

        assert sample is not None
        assert sample.psi_mem_some_avg10 == pytest.approx(12.50)
        assert sample.psi_mem_full_avg10 is None

    def test_malformed_lines_are_skipped_and_the_rest_parses(self, tmp_path):
        meminfo = (
            "MemTotal:        8129380 kB\n"
            "GarbageWithNoColon\n"
            "MemAvailable:    not-a-number kB\n"
            "Shmem:           4641344 kB\n"
            "Cached:\n"
            "SwapTotal:             0 kB\n"
        )
        pressure = (
            "some avg10=87.20 avg60=nonsense total=1\n"
            "full avg60=1.0\n"  # no avg10 at all
            "unexpected line\n"
        )
        proc = build_proc(
            tmp_path / "proc",
            meminfo=meminfo,
            pressure_memory=pressure,
            loadavg="garbage\n",
        )
        sample = host_pressure.read_sample(proc)

        assert sample is not None
        assert sample.mem_total_kb == 8129380
        assert sample.shmem_kb == 4641344
        assert sample.swap_total_kb == 0
        # Unparseable fields fall back to 0 rather than taking the read down.
        assert sample.mem_available_kb == 0
        assert sample.cached_kb == 0
        assert sample.psi_mem_some_avg10 == pytest.approx(87.20)
        assert sample.psi_mem_full_avg10 is None
        assert sample.load1 is None

    def test_truncated_meminfo_does_not_raise(self, tmp_path):
        proc = build_proc(tmp_path / "proc", meminfo="MemTotal:        81293")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert sample.mem_total_kb == 81293


# ---------------------------------------------------------------------------
# read_tmpfs_usage
# ---------------------------------------------------------------------------

MOUNTS = """\
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /dev/shm tmpfs rw,nosuid,nodev,size=4064688k 0 0
tmpfs /run tmpfs rw,nosuid,nodev,size=813000k,mode=755 0 0
tmpfs /run/user/1000 tmpfs rw,nosuid,nodev,relatime,size=812936k 0 0
tmpfs /mnt/odd\\040name tmpfs rw,relatime,size=1024k 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
"""


class TestReadTmpfsUsage:
    def test_returns_only_tmpfs_mounts_with_size_and_used(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(MOUNTS)
        statvfs = fake_statvfs(
            {
                "/dev/shm": (4064688 * 1024, 2_400_000 * 1024),
                "/run": (813000 * 1024, 1024 * 1024),
                "/run/user/1000": (812936 * 1024, 0),
                "/mnt/odd name": (1024 * 1024, 512 * 1024),
            }
        )

        stat = fake_stat(
            {"/dev/shm": 66, "/run": 67, "/run/user/1000": 68, "/mnt/odd name": 69}
        )

        usage = host_pressure.read_tmpfs_usage(mounts, statvfs=statvfs, stat=stat)

        assert [u.mount_point for u in usage] == [
            "/dev/shm",
            "/run",
            "/run/user/1000",
            "/mnt/odd name",
        ]
        assert [u.device_id for u in usage] == [66, 67, 68, 69]
        assert usage[0].size_bytes == 4064688 * 1024
        assert usage[0].used_bytes == 2_400_000 * 1024
        assert usage[1].used_bytes == 1024 * 1024
        assert usage[2].used_bytes == 0

    def test_a_mount_that_cannot_be_statted_is_skipped(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(MOUNTS)
        statvfs = fake_statvfs({"/run": (813000 * 1024, 1024 * 1024)})

        usage = host_pressure.read_tmpfs_usage(mounts, statvfs=statvfs)

        assert [u.mount_point for u in usage] == ["/run"]

    def test_missing_mounts_file_returns_empty(self, tmp_path):
        assert host_pressure.read_tmpfs_usage(tmp_path / "nope") == []

    def test_a_mount_whose_device_is_unknown_gets_minus_one(self, tmp_path):
        """Best-effort: a failed stat opts the row out of dedup, it does not
        drop the row — the usage figure itself is still good."""
        mounts = tmp_path / "mounts"
        mounts.write_text("tmpfs /run tmpfs rw 0 0\n")
        statvfs = fake_statvfs({"/run": (1024 * 1024, 4096)})

        usage = host_pressure.read_tmpfs_usage(mounts, statvfs=statvfs, stat=fake_stat({}))

        assert len(usage) == 1
        assert usage[0].device_id == -1
        assert usage[0].used_bytes == 4096

    def test_short_lines_are_skipped(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("tmpfs\ntmpfs /run tmpfs rw 0 0\n")
        statvfs = fake_statvfs({"/run": (1024 * 1024, 0)})
        usage = host_pressure.read_tmpfs_usage(mounts, statvfs=statvfs)
        assert [u.mount_point for u in usage] == ["/run"]


# ---------------------------------------------------------------------------
# shmem_unaccounted_kb — the field the whole module exists for
# ---------------------------------------------------------------------------


class TestShmemUnaccounted:
    def _sample(self, shmem_kb: int):
        return host_pressure.PressureSample(
            mem_total_kb=8129380,
            mem_available_kb=296284,
            shmem_kb=shmem_kb,
            swap_total_kb=0,
            swap_free_kb=0,
            cached_kb=1508,
            psi_mem_some_avg10=87.2,
            psi_mem_full_avg10=60.0,
            psi_io_some_avg10=39.1,
            psi_cpu_some_avg10=0.0,
            load1=76.12,
        )

    def test_large_residue_when_no_mount_holds_it(self):
        """The incident's shape: 4.64 GB of Shmem, near-empty tmpfs mounts.

        Nothing on the box can be ``du``'d to find this, which is what sends
        the search to ``/proc/*/fd`` instead.
        """
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 400_000 * 1024),
            host_pressure.TmpfsUsage("/run", 813000 * 1024, 41_344 * 1024),
        ]
        residue = host_pressure.shmem_unaccounted_kb(self._sample(4_641_344), tmpfs)
        assert residue == 4_200_000

    def test_near_zero_when_a_tmpfs_holds_all_of_it(self):
        """The other case: a mount is named, so there is a writer to find."""
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 4_641_000 * 1024),
        ]
        residue = host_pressure.shmem_unaccounted_kb(self._sample(4_641_344), tmpfs)
        assert residue == 344

    def test_floors_at_zero_rather_than_reporting_a_negative(self):
        """Shmem and the tmpfs sum are read at different instants, and a
        container mount can be counted by both."""
        tmpfs = [host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 9_000_000 * 1024)]
        assert host_pressure.shmem_unaccounted_kb(self._sample(4_641_344), tmpfs) == 0

    def test_no_tmpfs_mounts_leaves_shmem_whole(self):
        assert host_pressure.shmem_unaccounted_kb(self._sample(84_992), []) == 84_992

    def test_one_filesystem_reached_by_two_paths_is_counted_once(self):
        """The false negative that would call off the search.

        ``statvfs`` on a bind mount reports the whole underlying filesystem, so
        without deduplication two paths onto one tmpfs double its usage; large
        enough and the floor renders the residue as 0, which reads as "every
        byte of shmem lives in a mount someone can du".
        """
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 4_000_000 * 1024, 66),
            host_pressure.TmpfsUsage("/mnt/bind", 4064688 * 1024, 4_000_000 * 1024, 66),
        ]

        assert host_pressure.tmpfs_accounted_kb(tmpfs) == 4_000_000
        assert host_pressure.shmem_unaccounted_kb(self._sample(4_641_344), tmpfs) == 641_344

    def test_distinct_filesystems_are_both_counted(self):
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 400_000 * 1024, 66),
            host_pressure.TmpfsUsage("/run", 813000 * 1024, 41_344 * 1024, 67),
        ]
        assert host_pressure.tmpfs_accounted_kb(tmpfs) == 441_344

    def test_rows_with_no_device_are_never_deduped_against_each_other(self):
        """-1 means "not determined", which is not evidence of sameness."""
        tmpfs = [
            host_pressure.TmpfsUsage("/a", 1024 * 1024, 1024 * 1024),
            host_pressure.TmpfsUsage("/b", 1024 * 1024, 1024 * 1024),
        ]
        assert host_pressure.tmpfs_accounted_kb(tmpfs) == 2048


# ---------------------------------------------------------------------------
# is_under_pressure
# ---------------------------------------------------------------------------


class TestIsUnderPressure:
    def test_true_for_the_incident(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert host_pressure.is_under_pressure(
            sample, psi_threshold=40.0, min_available_mb=768
        )

    def test_false_for_the_post_reboot_host(self, tmp_path):
        proc = build_proc(
            tmp_path / "proc",
            meminfo=HEALTHY_MEMINFO,
            pressure_memory=HEALTHY_PRESSURE_MEMORY,
            loadavg="0.31 0.44 0.52 2/402 1204\n",
        )
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert not host_pressure.is_under_pressure(
            sample, psi_threshold=40.0, min_available_mb=768
        )

    def test_low_available_alone_is_enough(self, tmp_path):
        proc = build_proc(
            tmp_path / "proc",
            meminfo=HEALTHY_MEMINFO.replace("4594688 kB", " 400000 kB"),
            pressure_memory=HEALTHY_PRESSURE_MEMORY,
        )
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert host_pressure.is_under_pressure(
            sample, psi_threshold=40.0, min_available_mb=768
        )

    def test_psi_exactly_at_the_threshold_is_not_pressure(self, tmp_path):
        proc = build_proc(
            tmp_path / "proc",
            meminfo=HEALTHY_MEMINFO,
            pressure_memory="some avg10=40.00 avg60=1 total=1\nfull avg10=0.00 total=0\n",
        )
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert not host_pressure.is_under_pressure(
            sample, psi_threshold=40.0, min_available_mb=768
        )


# ---------------------------------------------------------------------------
# breadcrumb — a data format with future readers, not log chatter
# ---------------------------------------------------------------------------


class TestBreadcrumb:
    def test_exact_line_for_the_incident(self, tmp_path):
        """Assert the literal string. This line will be grepped and parsed
        after the fact, possibly by a future version of itself, so a silent
        reordering or rename is a defect and not a cosmetic change."""
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 400_000 * 1024),
            host_pressure.TmpfsUsage("/run", 813000 * 1024, 41_344 * 1024),
        ]

        line = host_pressure.breadcrumb(sample, tmpfs)

        assert line == (
            "host_pressure "
            "mem_total_kb=8129380 "
            "mem_available_kb=296284 "
            "shmem_kb=4641344 "
            "shmem_unaccounted_kb=4200000 "
            "tmpfs_sum_kb=441344 "
            "swap_total_kb=0 "
            "swap_free_kb=0 "
            "cached_kb=1508 "
            "psi_mem_some_avg10=87.20 "
            "psi_mem_full_avg10=60.00 "
            "psi_io_some_avg10=39.10 "
            "psi_cpu_some_avg10=0.00 "
            "load1=76.12 "
            "tmpfs_used_kb=/dev/shm:400000,/run:41344"
        )

    def test_renders_the_incident_shmem_and_zero_swap_without_truncation(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        line = host_pressure.breadcrumb(sample, [])

        assert "shmem_kb=4641344" in line
        assert "swap_total_kb=0" in line
        assert "swap_free_kb=0" in line
        # No thousands separators, no MB/GB rounding, no scientific notation.
        assert "4,641,344" not in line
        assert "4.6" not in line

    def test_stays_on_one_line_with_a_dozen_tmpfs_mounts(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        tmpfs = [
            host_pressure.TmpfsUsage(f"/run/user/{1000 + i}", 1024 * 1024, i * 1024)
            for i in range(12)
        ]

        line = host_pressure.breadcrumb(sample, tmpfs)

        assert "\n" not in line
        assert line.count("tmpfs_used_kb=") == 1
        assert "/run/user/1011:11" in line

    def test_no_tmpfs_mounts_renders_a_placeholder_not_an_empty_value(self, tmp_path):
        """An empty value would make the line ambiguous to split on."""
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        assert host_pressure.breadcrumb(sample, []).endswith("tmpfs_used_kb=-")

    def test_unmeasured_psi_renders_as_a_question_mark_not_zero(self, tmp_path):
        proc = build_proc(
            tmp_path / "proc",
            pressure_memory=None,
            pressure_io=None,
            pressure_cpu=None,
            loadavg=None,
        )
        sample = host_pressure.read_sample(proc)
        assert sample is not None

        line = host_pressure.breadcrumb(sample, [])

        assert "psi_mem_some_avg10=?" in line
        assert "psi_mem_full_avg10=?" in line
        assert "psi_io_some_avg10=?" in line
        assert "psi_cpu_some_avg10=?" in line
        assert "load1=?" in line
        assert "0.00" not in line
        # The figures the stage exists for are still there.
        assert "shmem_kb=4641344" in line

    def test_the_rendered_fields_are_arithmetically_consistent(self, tmp_path):
        """A reader recomputing the residue from the printed fields must get
        the printed residue back, so truncation happens in one place."""
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        tmpfs = [
            host_pressure.TmpfsUsage(f"/run/user/{1000 + i}", 1024 * 1024, 1023)
            for i in range(5)
        ]

        line = host_pressure.breadcrumb(sample, tmpfs)
        fields = dict(p.split("=", 1) for p in line.split(" ")[1:])
        per_mount = sum(
            int(e.rpartition(":")[2]) for e in fields["tmpfs_used_kb"].split(",")
        )

        assert int(fields["tmpfs_sum_kb"]) == per_mount
        assert int(fields["shmem_unaccounted_kb"]) == int(fields["shmem_kb"]) - per_mount

    def test_every_field_is_a_key_equals_value_pair(self, tmp_path):
        """Run this over mount points that fight the format, not over ``[]``.

        A space in a mount point is not exotic: the kernel escapes it in the
        mount table and ``read_tmpfs_usage`` unescapes it again so ``statvfs``
        can use it, so the unescaped form is what reaches the renderer. Comma
        and colon are not escaped by the kernel at all and are both delimiters
        here. Asserting the invariant only on an empty list asserts it in the
        one case where it cannot break.
        """
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        tmpfs = [
            host_pressure.TmpfsUsage("/mnt/odd name", 1024 * 1024, 512 * 1024),
            host_pressure.TmpfsUsage("/mnt/a,b", 1024 * 1024, 1024),
            host_pressure.TmpfsUsage("/mnt/a:b", 1024 * 1024, 1024),
            host_pressure.TmpfsUsage("/mnt/a=b", 1024 * 1024, 1024),
            host_pressure.TmpfsUsage("/mnt/back\\slash", 1024 * 1024, 1024),
        ]

        line = host_pressure.breadcrumb(sample, tmpfs)
        parts = line.split(" ")

        assert parts[0] == "host_pressure"
        for part in parts[1:]:
            assert part.count("=") >= 1, part
        # The tmpfs list is one field, and splitting it on "," then ":" from
        # the right yields exactly five mounts.
        value = line.split("tmpfs_used_kb=")[1]
        assert len(value.split(",")) == 5
        for entry in value.split(","):
            mount, _, used = entry.rpartition(":")
            assert mount
            assert used.isdigit()

    def test_a_mount_point_with_a_space_survives_a_round_trip(self, tmp_path):
        """Escaped on the way out, and back to the original on the way in."""
        proc = build_proc(tmp_path / "proc")
        sample = host_pressure.read_sample(proc)
        assert sample is not None
        tmpfs = [host_pressure.TmpfsUsage("/mnt/odd name", 1024 * 1024, 512 * 1024)]

        line = host_pressure.breadcrumb(sample, tmpfs)

        assert "tmpfs_used_kb=/mnt/odd\\040name:512" in line
        rendered = line.split("tmpfs_used_kb=")[1].rpartition(":")[0]
        assert host_pressure._unescape_mount(rendered) == "/mnt/odd name"


# ---------------------------------------------------------------------------
# read_process_rss
# ---------------------------------------------------------------------------


class TestReadProcessRss:
    def test_orders_by_rss_descending_and_honours_top_n(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        add_process(proc, 10, name="small", rss_kb=1024)
        add_process(proc, 11, name="claude", rss_kb=812_004, rss_shmem_kb=40)
        add_process(proc, 12, name="chrome", rss_kb=118_400, rss_shmem_kb=4)

        rows = host_pressure.read_process_rss(proc, top_n=2)

        assert [r.comm for r in rows] == ["claude", "chrome"]
        assert rows[0].pid == 11
        assert rows[0].rss_kb == 812_004
        assert rows[0].rss_shmem_kb == 40

    def test_tolerates_a_process_that_vanished_mid_scan(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        add_process(proc, 10, name="alive", rss_kb=2048)
        gone = proc / "11"
        gone.mkdir()  # directory listed, but no status file to read

        rows = host_pressure.read_process_rss(proc)

        assert [r.pid for r in rows] == [10]

    def test_kernel_threads_without_vmrss_are_omitted(self, tmp_path):
        proc = build_proc(tmp_path / "proc")
        add_process(proc, 10, name="kthreadd", rss_kb=None)
        add_process(proc, 11, name="real", rss_kb=512)

        rows = host_pressure.read_process_rss(proc)

        assert [r.comm for r in rows] == ["real"]

    def test_non_numeric_entries_in_proc_are_ignored(self, tmp_path):
        """``/proc/self`` would otherwise parse: it is a real directory with a
        real ``status`` file carrying a real ``VmRSS``, so only the ``isdigit``
        filter keeps it out. Asserting against a tree whose non-numeric entries
        are all unreadable would pass with no filter at all.
        """
        proc = build_proc(tmp_path / "proc")
        add_process(proc, 10, name="real", rss_kb=512)
        selfdir = proc / "self"
        selfdir.mkdir(exist_ok=True)
        (selfdir / "status").write_text("Name:\tself\nVmRSS:\t 999999 kB\n")

        rows = host_pressure.read_process_rss(proc)

        assert [r.pid for r in rows] == [10]
        assert "self" not in [r.comm for r in rows]


# ---------------------------------------------------------------------------
# count_memfd_by_process
# ---------------------------------------------------------------------------


class TestCountMemfdByProcess:
    def test_counts_memfd_symlinks_per_process(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(
            proc,
            10,
            name="leaky",
            rss_kb=4096,
            fds={
                "0": "/dev/null",
                "3": "/memfd:pytorch (deleted)",
                "4": "/memfd:whatever (deleted)",
                "5": "/etc/hosts",
            },
        )
        add_process(proc, 11, name="quiet", rss_kb=2048, fds={"0": "/dev/null"})

        rows = host_pressure.count_memfd_by_process(proc)

        assert rows == [(10, "leaky", 2)]

    def test_counts_deleted_files_under_a_tmpfs(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(
            proc,
            10,
            name="shmwriter",
            rss_kb=4096,
            fds={
                "3": "/dev/shm/blob.1 (deleted)",
                "4": "/var/tmp/ondisk.1 (deleted)",  # not a tmpfs, not counted
                "5": "/dev/shm/live",  # still linked, not counted
            },
        )

        rows = host_pressure.count_memfd_by_process(proc)

        assert rows == [(10, "shmwriter", 1)]

    def test_skips_a_process_whose_fd_dir_is_unreadable(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="opaque", rss_kb=4096, fd_unreadable=True)
        add_process(proc, 11, name="leaky", rss_kb=4096, fds={"3": "/memfd:x (deleted)"})

        rows = host_pressure.count_memfd_by_process(proc)

        assert rows == [(11, "leaky", 1)]

    def test_sorted_by_count_descending(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="one", rss_kb=1, fds={"3": "/memfd:a (deleted)"})
        add_process(
            proc,
            11,
            name="three",
            rss_kb=1,
            fds={str(i): f"/memfd:{i} (deleted)" for i in range(3, 6)},
        )

        rows = host_pressure.count_memfd_by_process(proc)

        assert [r[0] for r in rows] == [11, 10]

    def test_empty_when_proc_is_absent(self, tmp_path):
        assert host_pressure.count_memfd_by_process(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_names_every_tmpfs_mount_with_its_used_bytes(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="claude", rss_kb=812_004, rss_shmem_kb=40)
        tmpfs = [
            host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 400_000 * 1024),
            host_pressure.TmpfsUsage("/run", 813000 * 1024, 41_344 * 1024),
            host_pressure.TmpfsUsage("/run/user/1000", 812936 * 1024, 0),
        ]

        text = host_pressure.snapshot(proc, tmpfs=tmpfs, containers=[])

        assert "tmpfs mount=/dev/shm size_kb=4064688 used_kb=400000" in text
        assert "tmpfs mount=/run size_kb=813000 used_kb=41344" in text
        assert "tmpfs mount=/run/user/1000 size_kb=812936 used_kb=0" in text
        assert "tmpfs_sum_kb=441344" in text

    def test_orders_the_process_table_by_rss_descending(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="small", rss_kb=1024)
        add_process(proc, 11, name="claude", rss_kb=812_004)
        add_process(proc, 12, name="chrome", rss_kb=118_400)

        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])
        order = [
            ln.split("comm=")[1].split(" ")[0]
            for ln in text.splitlines()
            if ln.strip().startswith("proc ")
        ]

        assert order == ["claude", "chrome", "small"]

    def test_reports_the_headline_memory_figures(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])

        assert "shmem_kb=4641344" in text
        assert "swap_total_kb=0" in text
        assert "mem_available_kb=296284" in text

    def test_says_how_many_processes_it_read(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="a", rss_kb=1)
        add_process(proc, 11, name="b", rss_kb=2)
        (proc / "12").mkdir()  # vanished mid-scan

        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])

        assert "processes_read=2" in text

    def test_tolerates_a_process_whose_status_is_missing(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="alive", rss_kb=4096)
        (proc / "11").mkdir()

        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])

        assert "comm=alive" in text

    def test_notes_what_it_could_not_read_rather_than_raising(self, tmp_path):
        proc = tmp_path / "nope"
        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])

        assert "host_pressure_snapshot" in text
        assert "sample=unavailable" in text

    def test_an_unreadable_sample_reports_no_residue_rather_than_zero(self, tmp_path):
        """Zero is a finding — "every byte is accounted for". Not measuring is
        not a finding, and the two must not render alike."""
        text = host_pressure.snapshot(tmp_path / "nope", tmpfs=[], containers=[])

        assert "shmem_unaccounted_kb=unavailable" in text
        assert "shmem_unaccounted_kb=0" not in text

    def test_a_process_name_containing_a_space_does_not_break_the_line(self, tmp_path):
        """Firefox's content processes are called ``Web Content``. The comm
        comes from the kernel, so the renderer has to make it safe."""
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="Web Content", rss_kb=4096, rss_shmem_kb=8)

        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])
        proc_line = next(ln for ln in text.splitlines() if ln.strip().startswith("proc "))

        assert "comm=Web\\040Content" in proc_line
        for field in proc_line.split():
            assert "=" in field or field == "proc"
        assert "rss_shmem_kb=8" in proc_line

    def test_a_tmpfs_mount_with_a_space_does_not_break_the_line(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        tmpfs = [host_pressure.TmpfsUsage("/mnt/odd name", 1024 * 1024, 512 * 1024)]

        text = host_pressure.snapshot(proc, tmpfs=tmpfs, containers=[])
        line = next(ln for ln in text.splitlines() if ln.strip().startswith("tmpfs mount="))

        assert "mount=/mnt/odd\\040name" in line
        assert "used_kb=512" in line

    def test_proc_root_scopes_the_whole_snapshot(self, tmp_path):
        """``--proc-root`` must not still stat real host paths or open the live
        Docker socket, or the flag is a half-truth."""
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(proc, 10, name="claude", rss_kb=4096)
        statvfs = fake_statvfs(
            {
                "/dev/shm": (4064688 * 1024, 400_000 * 1024),
                "/run": (813000 * 1024, 41_344 * 1024),
                "/run/user/1000": (812936 * 1024, 0),
                "/mnt/odd name": (1024 * 1024, 512 * 1024),
            }
        )

        # Both defaults exercised: tmpfs read from the fixture mount table, and
        # a docker socket that cannot exist.
        text = host_pressure.snapshot(
            proc, statvfs=statvfs, docker_socket=tmp_path / "no-such.sock"
        )

        assert "tmpfs mount=/dev/shm size_kb=4064688 used_kb=400000" in text
        assert "container name=? mount=? used_kb=unavailable" in text
        assert "comm=claude" in text

    def test_walks_proc_fd_when_the_residue_is_large(self, tmp_path):
        """The one case where nothing else in the snapshot can point at a
        culprit: shmem that lives in no filesystem at all."""
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        add_process(
            proc,
            10,
            name="leaky",
            rss_kb=4096,
            fds={"3": "/memfd:blob (deleted)", "4": "/memfd:blob2 (deleted)"},
        )

        text = host_pressure.snapshot(proc, tmpfs=[], containers=[])

        assert "memfd pid=10 comm=leaky count=2" in text

    def test_skips_the_fd_walk_when_the_residue_is_small(self, tmp_path):
        """The fd walk is the only part of the module that costs more than a
        few file reads, so it stays off unless the residue calls for it."""
        proc = build_proc(
            tmp_path / "proc",
            meminfo=HEALTHY_MEMINFO,
            pressure_memory=HEALTHY_PRESSURE_MEMORY,
            mounts=MOUNTS,
        )
        add_process(proc, 10, name="leaky", rss_kb=4096, fds={"3": "/memfd:b (deleted)"})
        tmpfs = [host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 84_000 * 1024)]

        text = host_pressure.snapshot(proc, tmpfs=tmpfs, containers=[])

        assert "memfd pid=" not in text

    def test_container_shm_unavailable_is_recorded_not_omitted(self, tmp_path):
        """An absent line and a zero line must not look alike — the whole point
        of the section is attribution."""
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        containers = [
            host_pressure.ContainerShmUsage(
                name="istota-browser",
                mount_point="/dev/shm",
                size_bytes=0,
                used_bytes=0,
                available=False,
                detail="container not running",
            )
        ]

        text = host_pressure.snapshot(proc, tmpfs=[], containers=containers)

        assert "container name=istota-browser mount=/dev/shm used_kb=unavailable" in text
        assert "container not running" in text

    def test_container_shm_available_reports_used_kb(self, tmp_path):
        proc = build_proc(tmp_path / "proc", mounts=MOUNTS)
        containers = [
            host_pressure.ContainerShmUsage(
                name="istota-browser",
                mount_point="/dev/shm",
                size_bytes=2 * 1024 * 1024 * 1024,
                used_bytes=64 * 1024 * 1024,
                available=True,
            )
        ]

        text = host_pressure.snapshot(proc, tmpfs=[], containers=containers)

        assert "container name=istota-browser mount=/dev/shm used_kb=65536" in text


# ---------------------------------------------------------------------------
# read_container_shm
# ---------------------------------------------------------------------------


class TestReadContainerShm:
    def test_unreachable_docker_socket_yields_one_unavailable_row(self, tmp_path):
        rows = host_pressure.read_container_shm(
            docker_socket=tmp_path / "no-such.sock", timeout=0.2
        )

        assert len(rows) == 1
        assert rows[0].available is False
        assert rows[0].detail
        # Never silently empty: an operator must be able to tell "docker was
        # unreachable" from "no container had an shm mount".
        assert rows[0].name == "?"

    def test_container_mounts_are_read_from_the_hosts_view_of_its_pid(self, tmp_path):
        """No exec, no subprocess: the container's tmpfs is reachable through
        ``/proc/<pid>/root`` because the kernel resolves it across namespaces."""
        proc = tmp_path / "proc"
        cproc = proc / "9001"
        (cproc / "root" / "dev" / "shm").mkdir(parents=True)
        (cproc / "mounts").write_text(
            "shm /dev/shm tmpfs rw,nosuid,nodev,size=2097152k 0 0\n"
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
        )
        statvfs = fake_statvfs(
            {str(cproc / "root" / "dev" / "shm"): (2097152 * 1024, 65536 * 1024)}
        )

        rows = host_pressure.container_shm_for_pid(
            "istota-browser", 9001, proc_root=proc, statvfs=statvfs
        )

        assert len(rows) == 1
        assert rows[0].name == "istota-browser"
        assert rows[0].mount_point == "/dev/shm"
        assert rows[0].available is True
        assert rows[0].used_bytes == 65536 * 1024

    def test_unreadable_container_mounts_yields_an_unavailable_row(self, tmp_path):
        rows = host_pressure.container_shm_for_pid(
            "devbox-alice", 9002, proc_root=tmp_path / "proc"
        )

        assert len(rows) == 1
        assert rows[0].available is False
        assert rows[0].name == "devbox-alice"

    def test_a_stopped_container_reports_unavailable(self, tmp_path):
        rows = host_pressure.container_shm_for_pid(
            "istota-browser", 0, proc_root=tmp_path / "proc"
        )

        assert len(rows) == 1
        assert rows[0].available is False
        assert "not running" in rows[0].detail
