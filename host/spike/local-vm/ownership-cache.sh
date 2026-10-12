#!/bin/bash
# local-vm-install Stage 1 (a): the guest caches the owner the server reported
# to whoever looked last. Does a uid 10001 write fail right after root looked,
# and what does a chown do? Run as root in the guest, after ownership-model.sh.
set -uo pipefail
W="${1:-/srv/mount/host-folder}"
O="$W/own"

as() { local u="$1"; shift; setpriv --reuid="$u" --regid="$u" --clear-groups "$@" 2>&1; }

echo "== root stats, then 10001 writes at once (x5)"
for i in 1 2 3 4 5; do
    stat -c 'root sees uid=%u' "$O/priv600.txt" "$O/privdir" >/dev/null
    echo "try $i: append=$(as 10001 sh -c "echo y >> '$O/priv600.txt' && echo ok") create=$(as 10001 sh -c "touch '$O/privdir/c$i' && echo ok") stat=$(as 10001 stat -c '%u' "$O/priv600.txt")"
done
echo "== after 1.5 s"
sleep 1.5
echo "append=$(as 10001 sh -c "echo y >> '$O/priv600.txt' && echo ok") stat=$(as 10001 stat -c '%u' "$O/priv600.txt")"

echo "== chown as root"
echo "chown 4242:4242 priv600: $(chown 4242:4242 "$O/priv600.txt" && echo ok)"
echo "root stat right after:  $(stat -c 'uid=%u gid=%g' "$O/priv600.txt")"
sleep 2
echo "root stat after 2 s:    $(stat -c 'uid=%u gid=%g' "$O/priv600.txt")"
echo "uid 1234 stat:          $(as 1234 stat -c 'uid=%u gid=%g' "$O/priv600.txt")"
echo "chmod 0640 as root: $(chmod 0640 "$O/priv600.txt" && echo ok); mode now $(stat -c %a "$O/priv600.txt")"
