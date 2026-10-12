#!/usr/bin/env python3
"""local-vm-install Stage 1 (a): the Mac half of the edit-visibility probe.

    latency-mac.py <workspace-dir> [rounds]

For each round: create a new file; rewrite edit.txt in place (same inode, as
`echo >` does); replace atomic.txt by rename (as most editors save). Measures
the round trip until the guest's ack appears on the Mac, and the one-way time
from the guest's own clock (both clocks are NTP-synced; the offset is printed
by the caller). The guest half is latency-watch.py, already running.
"""
import os
import sys
import time

D = os.path.join(sys.argv[1], "lat")
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
TIMEOUT = 60.0


def wait_ack(name):
    path = os.path.join(D, f"ack-{name}")
    start = time.time()
    while time.time() - start < TIMEOUT:
        try:
            with open(path) as f:
                parts = f.read().split()
            return time.time(), float(parts[-1])
        except (OSError, ValueError, IndexError):
            time.sleep(0.005)
    return None, None


def measure(kind, name, action):
    t0 = time.time()
    action()
    t1, guest_seen = wait_ack(name)
    if t1 is None:
        print(f"{kind}: no ack within {TIMEOUT:.0f} s")
        return
    print(f"{kind}: round_trip={1000 * (t1 - t0):.0f} ms  mac_to_guest={1000 * (guest_seen - t0):.0f} ms")


def write(path, value):
    with open(path, "w") as f:
        f.write(value + "\n")


def atomic(path, value):
    tmp = path + ".tmp"
    write(tmp, value)
    os.rename(tmp, path)


for r in range(1, ROUNDS + 1):
    measure("create      ", f"new-{r}", lambda: write(os.path.join(D, f"new-{r}"), "x"))
    time.sleep(0.5)
    measure("edit-in-place", f"edit.txt-v{r}", lambda: write(os.path.join(D, "edit.txt"), f"v{r}"))
    time.sleep(0.5)
    measure("atomic-rename", f"atomic.txt-v{r}", lambda: atomic(os.path.join(D, "atomic.txt"), f"v{r}"))
    time.sleep(0.5)
write(os.path.join(D, "stop"), "1")
