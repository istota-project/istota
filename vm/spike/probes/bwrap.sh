#!/bin/bash
# bwrap cases, run as whoever execs this (the driver runs it as 10001).
# Each prints one CASE line with the exit status and the first stderr line.
set -uo pipefail

run_case() {
    local name="$1"; shift
    local out err rc
    out="$(mktemp)"; err="$(mktemp)"
    "$@" >"$out" 2>"$err"
    rc=$?
    echo "CASE ${name} exit=${rc} stderr=$(head -1 "$err") stdout=$(head -c 300 "$out" | tr '\n' '|')"
    rm -f "$out" "$err"
}

echo "uid=$(id -u) bwrap=$(bwrap --version)"

run_case userns bwrap --unshare-user --ro-bind / / -- /bin/true
run_case userns_proc_dev bwrap --unshare-user --ro-bind / / --proc /proc --dev /dev -- /bin/true
# The plan's unconditional set: pid ns, procfs, dev, a read-only tmpfs mask,
# --disable-userns, --die-with-parent.
run_case plan_shape bwrap --unshare-user --disable-userns --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    --tmpfs /data/db --remount-ro /data/db \
    -- /bin/sh -c 'ls /data/db | wc -l; touch /data/db/x 2>&1 | head -1; cat /proc/self/uid_map'
# The network namespace: only loopback, and an outbound connect fails.
run_case unshare_net bwrap --unshare-user --unshare-net --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    -- /usr/bin/python3 -c '
import socket
print("ifaces", sorted(n for _, n in socket.if_nameindex()))
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("1.1.1.1", 443)); print("connect ok")
except OSError as e:
    print("connect", type(e).__name__, e.errno)
'
# Outside bwrap, the same connect works (control for unshare_net).
run_case outside_net_control /usr/bin/python3 -c '
import socket
s = socket.socket(); s.settimeout(5)
try:
    s.connect(("1.1.1.1", 443)); print("connect ok")
except OSError as e:
    print("connect", type(e).__name__, e.errno)
'
# Syscalls from inside a sandbox: the filter is inherited.
run_case syscalls_in_sandbox bwrap --unshare-user --unshare-pid --die-with-parent \
    --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
    -- /usr/bin/python3 /spike/probes/syscalls.py
# The executor's own probe, in-process.
run_case executor_bwrap_available /app/.venv/bin/python -c '
from istota import executor
print("available", executor._bwrap_available(), "needs_unshare_user", executor._bwrap_needs_unshare_user)
'
