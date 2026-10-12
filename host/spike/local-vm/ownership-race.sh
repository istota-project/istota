#!/bin/bash
# local-vm-install Stage 1 (a): after root's setattr leaves owner 0 in the
# guest's attribute cache, can uid 10001 still write a 0640 file, and when does
# the cached owner flip back? Run as root in the guest.
set -uo pipefail
W="${1:-/srv/mount/host-folder}"
F="$W/own/race.txt"
as() { local u="$1"; shift; setpriv --reuid="$u" --regid="$u" --clear-groups "$@" 2>&1; }

echo r > "$F"
for delay in 0 0.5 1 2 5 10; do
    chmod 0640 "$F"            # root's setattr: the reply caches owner 0
    sleep "$delay"
    w="$(as 10001 sh -c "echo y >> '$F' && echo ok")"
    s="$(as 10001 stat -c '%u' "$F")"
    echo "delay=${delay}s write_as_10001=${w} owner_seen_by_10001=${s}"
done
echo "== a 0755 directory root just touched"
D="$W/own/racedir"; mkdir -p "$D"
for delay in 0 1 5; do
    chmod 0755 "$D"
    sleep "$delay"
    echo "delay=${delay}s create_as_10001=$(as 10001 sh -c "touch '$D/f$delay' && echo ok")"
done
