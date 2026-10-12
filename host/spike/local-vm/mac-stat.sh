#!/bin/bash
# local-vm-install Stage 1 (a): the Mac-side owner, mode and mtime of every
# entry in the workspace folder, after the guest wrote into it. Run on the Mac.
#
#   mac-stat.sh <workspace-dir>
set -uo pipefail
cd "$1" || exit 1
find . -mindepth 1 -print0 | sort -z | while IFS= read -r -d '' p; do
    printf '%s  ' "$(stat -f '%Su:%Sg %Lp %Sm' "$p")"
    printf '%s' "$p"
    x="$(xattr "$p" 2>/dev/null | tr '\n' ',')"
    [ -n "$x" ] && printf '  xattrs=%s' "$x"
    printf '\n'
done
