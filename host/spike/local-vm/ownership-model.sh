#!/bin/bash
# local-vm-install Stage 1 (a): what owner Apple's virtiofs server reports, to
# whom, and whether mode bits are enforced. Run as root in the guest after the
# Mac has created, in the workspace root:
#   own/priv600.txt (0600)   own/ro444.txt (0444)   own/privdir/ (0700) with f.txt
set -uo pipefail
W="${1:-/srv/mount/host-folder}"
O="$W/own"

as() { local u="$1"; shift; setpriv --reuid="$u" --regid="$u" --clear-groups "$@" 2>&1; }

for u in 4242 0 10001 4242; do
    echo "== uid $u (stat via python, after 2 s)"
    sleep 2
    as "$u" python3 -c "
import os, sys
for p in ['$O', '$O/priv600.txt', '$O/ro444.txt', '$O/privdir', '$O/privdir/f.txt']:
    try:
        s = os.stat(p); print(f'{p}: uid={s.st_uid} gid={s.st_gid} mode={oct(s.st_mode & 0o7777)}')
    except OSError as e:
        print(f'{p}: {e}')
"
done

echo "== access as uid 4242 (a stranger to both sides)"
echo "read priv600:  $(as 4242 cat "$O/priv600.txt")"
echo "write priv600: $(as 4242 sh -c "echo x >> '$O/priv600.txt' && echo ok")"
echo "write ro444:   $(as 4242 sh -c "echo x >> '$O/ro444.txt' && echo ok")"
echo "list privdir:  $(as 4242 ls "$O/privdir" | tr '\n' ' ')"
echo "create in privdir: $(as 4242 sh -c "touch '$O/privdir/by4242' && echo ok")"
echo "chmod ro444 to 644 as 4242: $(as 4242 chmod 644 "$O/ro444.txt" && echo ok)"
echo "== the mount's options"
grep " $W " /proc/self/mountinfo
