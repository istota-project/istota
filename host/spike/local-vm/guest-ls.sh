#!/bin/bash
# local-vm-install Stage 1: what the guest sees at the workspace mount right
# now, and whether a write lands. Used around Mac-side renames and deletes of
# the shared folder. Run as root in the guest.
#   guest-ls.sh <tag>
set -uo pipefail
W=/srv/mount/host-folder
echo "marker: $(test -e "$W/.istota-workspace" && echo present || echo missing)"
echo "entries: $(ls -A "$W" 2>&1 | head -8 | tr '\n' ' ')"
echo "write: $(setpriv --reuid=10001 --regid=10001 --clear-groups sh -c "echo '$1' > '$W/written-$1.txt' && echo ok" 2>&1)"
echo "mount: $(findmnt -n -o FSTYPE,SOURCE "$W")"
