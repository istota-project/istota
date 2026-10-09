#!/usr/bin/env python3
"""Try the root phase's cgroup remount routes with raw mount(2); print errno.

Run as root (a plain `docker compose exec`) in a container whose root phase
skipped the cgroup step, so the mount is still Docker's read-only one.
"""

import ctypes
import errno
import os

libc = ctypes.CDLL(None, use_errno=True)
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_RELATIME = 32, 4096, 1 << 21
MNT_DETACH = 2
CG = b"/sys/fs/cgroup"


def mount(source, target, fstype, flags, data=None) -> str:
    rc = libc.mount(source, target, fstype, ctypes.c_ulong(flags), data)
    if rc == 0:
        return "ok"
    return errno.errorcode.get(ctypes.get_errno(), "?")


def mount_opts() -> str:
    with open("/proc/self/mountinfo") as f:
        for line in f:
            fields = line.split()
            if fields[4] == "/sys/fs/cgroup":
                sep = fields.index("-")
                return f"root={fields[3]} mnt_opts={fields[5]} type={fields[sep + 1]} super_opts={fields[sep + 3]}"
    return "no mount"


print(f"uid={os.getuid()} before: {mount_opts()}")
base = MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_RELATIME
print(f"remount (superblock + mount flags) rw: {mount(None, CG, None, MS_REMOUNT | base)}")
print(f"  after: {mount_opts()}")
print(f"remount bind rw (per-mount flags only): {mount(None, CG, None, MS_REMOUNT | MS_BIND | base)}")
print(f"  after: {mount_opts()}")
print(f"fresh cgroup2 over it (no umount): {mount(b'cgroup2', CG, b'cgroup2', base)}")
print(f"  after: {mount_opts()}")
with open("/proc/self/cgroup") as f:
    print(f"/proc/self/cgroup: {f.read().strip()}")
