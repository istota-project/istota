#!/usr/bin/env python3
"""local-vm-install Stage 1 (a): the guest half of the edit-visibility probe.

Runs as uid 10001 in the istota container with the workspace at /mnt/shared.
Polls lat/ every 10 ms; when it sees a change the Mac made, it records its own
wall clock and writes an ack file the Mac is polling for. latency-mac.py is the
other half. Stops when lat/stop appears.
"""
import os
import time

D = "/mnt/shared/lat"
os.makedirs(D, exist_ok=True)
seen = {}


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def ack(name, value):
    tmp = os.path.join(D, f".ack-{name}.tmp")
    with open(tmp, "w") as f:
        f.write(f"{value} {time.time():.6f}\n")
    os.rename(tmp, os.path.join(D, f"ack-{name}"))


# Prime the cache on the files the Mac will edit in place, as a daemon that
# had already read them would.
for name in ("edit.txt", "atomic.txt"):
    read(os.path.join(D, name))

while not os.path.exists(os.path.join(D, "stop")):
    for entry in os.listdir(D):
        if entry.startswith("new-") and entry not in seen:
            seen[entry] = True
            ack(entry, "created")
    for name in ("edit.txt", "atomic.txt"):
        value = read(os.path.join(D, name))
        if value and seen.get(name) != value:
            seen[name] = value
            ack(f"{name}-{value}", value)
    time.sleep(0.01)
print("watcher stopped")
