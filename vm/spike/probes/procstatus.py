#!/usr/bin/env python3
"""Print identity, capability sets, no_new_privs, seccomp and LSM label.

Usage: procstatus.py [self|daemon|<pid> ...]
`daemon` finds the istota-scheduler process by its command line.
"""

import os
import sys

CAP_NAMES = [
    "CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "FSETID", "KILL",
    "SETGID", "SETUID", "SETPCAP", "LINUX_IMMUTABLE", "NET_BIND_SERVICE",
    "NET_BROADCAST", "NET_ADMIN", "NET_RAW", "IPC_LOCK", "IPC_OWNER",
    "SYS_MODULE", "SYS_RAWIO", "SYS_CHROOT", "SYS_PTRACE", "SYS_PACCT",
    "SYS_ADMIN", "SYS_BOOT", "SYS_NICE", "SYS_RESOURCE", "SYS_TIME",
    "SYS_TTY_CONFIG", "MKNOD", "LEASE", "AUDIT_WRITE", "AUDIT_CONTROL",
    "SETFCAP", "MAC_OVERRIDE", "MAC_ADMIN", "SYSLOG", "WAKE_ALARM",
    "BLOCK_SUSPEND", "AUDIT_READ", "PERFMON", "BPF", "CHECKPOINT_RESTORE",
]
FIELDS = ("Uid", "Gid", "Groups", "CapInh", "CapPrm", "CapEff", "CapBnd",
          "CapAmb", "NoNewPrivs", "Seccomp", "Seccomp_filters")


def decode(hexmask: str) -> str:
    value = int(hexmask, 16)
    names = [n for i, n in enumerate(CAP_NAMES) if value >> i & 1]
    return ",".join(names) if names else "-"


def find_daemon() -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
        except OSError:
            continue
        if b"istota-scheduler" in cmd and b"--daemon" in cmd:
            return int(entry)
    return None


def report(target: str) -> None:
    if target == "daemon":
        pid = find_daemon()
        if pid is None:
            print("daemon: NOT RUNNING")
            return
    elif target == "self":
        pid = os.getpid()
    else:
        pid = int(target)
    status = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            key, _, value = line.partition(":")
            status[key] = value.strip()
    try:
        with open(f"/proc/{pid}/attr/current") as f:
            lsm = f.read().strip()
    except OSError as exc:
        lsm = f"unreadable ({exc.strerror})"
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    print(f"{target} pid={pid} cmd={cmd[:80]!r}")
    for key in FIELDS:
        value = status.get(key, "?")
        if key.startswith("Cap"):
            value = f"{value} [{decode(value)}]"
        print(f"  {key}: {value}")
    print(f"  apparmor: {lsm}")


def main() -> int:
    for target in sys.argv[1:] or ["self"]:
        report(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
