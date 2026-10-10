#!/bin/bash
# What a process can touch outside its own tree: the secret file's mode as
# seen from inside, /proc/sys writability, a Docker socket, the read-only root.
set -uo pipefail

echo "uid=$(id -u)"
for f in /run/secrets/*; do
    [ -e "$f" ] && echo "secret $f: $(stat -c 'uid=%u gid=%g mode=%a' "$f")"
done
echo "/proc/sys mount: $(awk '$5 == "/proc/sys" {print $6}' /proc/self/mountinfo | head -1) (absent line = part of /proc)"
swappiness="$(cat /proc/sys/vm/swappiness)"
if (echo "$swappiness" > /proc/sys/vm/swappiness) 2>/dev/null; then
    echo "write same value to /proc/sys/vm/swappiness: SUCCEEDED (VM sysctl writable)"
else
    echo "write same value to /proc/sys/vm/swappiness: refused"
fi
if (echo "$(cat /proc/sys/kernel/core_pattern)" > /proc/sys/kernel/core_pattern) 2>/dev/null; then
    echo "write same value to /proc/sys/kernel/core_pattern: SUCCEEDED"
else
    echo "write same value to /proc/sys/kernel/core_pattern: refused"
fi
if [ -e /proc/sysrq-trigger ]; then
    echo "/proc/sysrq-trigger: $(stat -c 'mode=%a' /proc/sysrq-trigger), writable=$( [ -w /proc/sysrq-trigger ] && echo yes || echo no)"
fi
for d in /app /usr /etc; do
    if touch "$d/.spike-write" 2>/dev/null; then echo "write $d: SUCCEEDED"; rm -f "$d/.spike-write"; else echo "write $d: refused"; fi
done
found="$(find / -xdev -name docker.sock 2>/dev/null | head -1)"
echo "docker.sock: ${found:-none}; DOCKER_HOST=${DOCKER_HOST:-unset}"
