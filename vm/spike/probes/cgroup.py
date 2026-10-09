#!/app/.venv/bin/python
"""Per-task cgroup probe, through the real istota.sandbox.cgroup module.

Run as 10001 after the root phase. Prints the cgroup2 mount's root from
mountinfo, the process's own cgroup, then drives probe(), create() with a
64 MiB memory.max, and a child that allocates 256 MiB placed with
placement(): it must be OOM-killed and memory.events must say so.
"""

import os
import subprocess
import sys
from pathlib import Path

from istota.sandbox import cgroup

ROOT = Path(os.environ.get("ISTOTA_TASK_CGROUP_ROOT", "/sys/fs/cgroup"))


def mount_line() -> str:
    with open("/proc/self/mountinfo") as f:
        for line in f:
            fields = line.split()
            if fields[4] == "/sys/fs/cgroup":
                sep = fields.index("-")
                return f"root={fields[3]} type={fields[sep + 1]} opts={fields[5]}"
    return "no mount at /sys/fs/cgroup"


def main() -> int:
    print(f"uid={os.getuid()} root={ROOT}")
    print(f"mountinfo: {mount_line()}")
    print(f"/proc/self/cgroup: {Path('/proc/self/cgroup').read_text().strip()}")
    print(f"root listing: {sorted(p.name for p in ROOT.iterdir() if p.is_dir())}")
    print(f"subtree_control: {(ROOT / 'cgroup.subtree_control').read_text().strip()!r}")
    print(f"resolve_root() (unpatched, no ISTOTA_TASK_CGROUP_ROOT arm yet): {cgroup.resolve_root()}")

    reason = cgroup.probe(ROOT)
    print(f"probe(): {'OK' if reason is None else reason}")
    if reason is not None:
        return 1

    limits = cgroup.CgroupLimits(memory_max_mb=64, pids_max=64, cpu_max_percent=50)
    path = cgroup.create(990001, limits, attempt=1, root=ROOT)
    print(f"create(): {path}")
    if path is None:
        return 1
    for name in ("memory.max", "pids.max", "cpu.max"):
        print(f"  {name} = {(path / name).read_text().strip()}")

    hog = "b = bytearray(256 * 1024 * 1024)\nfor i in range(0, len(b), 4096): b[i] = 1\nprint('survived')"
    with cgroup.placement(path) as preexec:
        proc = subprocess.run([sys.executable, "-c", hog], preexec_fn=preexec,
                              capture_output=True, text=True)
    print(f"256 MiB child under 64 MiB: returncode={proc.returncode} stdout={proc.stdout.strip()!r}")
    print(f"  memory.events: {cgroup.read_events(path)}")

    small = "b = bytearray(16 * 1024 * 1024)\nfor i in range(0, len(b), 4096): b[i] = 1\nprint('survived')"
    with cgroup.placement(path) as preexec:
        proc = subprocess.run([sys.executable, "-c", small], preexec_fn=preexec,
                              capture_output=True, text=True)
    print(f"16 MiB child under 64 MiB: returncode={proc.returncode} stdout={proc.stdout.strip()!r}")
    print(f"destroy(): {cgroup.destroy(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
