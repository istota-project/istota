#!/bin/bash
# local-vm-install Stage 1 (h): `limactl autostart` on and off for one
# instance, and what it leaves on the Mac. Run on the Mac.
#   autostart.sh <instance>
set -uo pipefail
INSTANCE="$1"
AGENTS="$HOME/Library/LaunchAgents"

show() {
    echo "-- launch agents naming lima:"
    find "$AGENTS" -maxdepth 1 -iname '*lima*' -print 2>/dev/null
    echo "-- launchctl list:"
    launchctl list | grep -i lima || echo "(none)"
}

echo "== help"
limactl autostart enable --help 2>&1 | sed -n '1,12p'
limactl autostart disable --help 2>&1 | sed -n '1,6p'
echo "== deprecated alias"
limactl start-at-login --help 2>&1 | sed -n '1,8p'
echo "== before"; show
echo "== enable"; limactl autostart enable "$INSTANCE"; echo "exit=$?"
show
for f in "$AGENTS"/*lima*"$INSTANCE"*; do [ -f "$f" ] && { echo "-- $f"; cat "$f"; }; done
echo "== status after enable: $(limactl list --format '{{.Name}} {{.Status}}' | grep "^$INSTANCE ")"
echo "== enable again (idempotent?)"; limactl autostart enable "$INSTANCE"; echo "exit=$?"
echo "== disable"; limactl autostart disable "$INSTANCE"; echo "exit=$?"
show
echo "== status after disable: $(limactl list --format '{{.Name}} {{.Status}}' | grep "^$INSTANCE ")"
echo "== disable again (idempotent?)"; limactl autostart disable "$INSTANCE"; echo "exit=$?"
