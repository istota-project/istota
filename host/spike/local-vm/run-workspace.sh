#!/bin/bash
# local-vm-install Stage 1 (a): run workspace-container.sh in the istota image
# under the daemon's run contract, with SOURCE bound at /mnt/shared (rslave)
# as compose's x-workspace-mount does. Run as root in the guest.
#
#   run-workspace.sh <source-dir> <tag> [image]
#
# The probe directory (this script's) must hold seccomp-istota.json and
# apparmor-istota copied from docker/istota/ at the commit under test.
set -uo pipefail

SOURCE="$1"
TAG="$2"
IMAGE="${3:-istota-test/lean:e74eebd3}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -d /sys/kernel/security/apparmor ]; then
    install -m 0644 "$HERE/apparmor-istota" /etc/apparmor.d/istota
    apparmor_parser -r /etc/apparmor.d/istota
fi

echo "== guest view of $SOURCE"
findmnt -T "$SOURCE" -o TARGET,SOURCE,FSTYPE,OPTIONS
stat -c '%n uid=%u gid=%g mode=%a' "$SOURCE" "$SOURCE/Users/alice/inbox/mac.txt"
echo "as uid 10001 on the VM, no container:"
setpriv --reuid=10001 --regid=10001 --clear-groups sh -c "touch '$SOURCE/vm-10001-$TAG.txt' && stat -c 'uid=%u gid=%g' '$SOURCE/vm-10001-$TAG.txt'" 2>&1
echo "as root on the VM:"
sh -c "touch '$SOURCE/vm-root-$TAG.txt' && stat -c 'uid=%u gid=%g' '$SOURCE/vm-root-$TAG.txt'" 2>&1

echo "== container run contract, uid 10001"
docker run --rm \
    --security-opt "seccomp=$HERE/seccomp-istota.json" \
    --security-opt apparmor=istota \
    --security-opt systempaths=unconfined \
    --security-opt no-new-privileges:true \
    --cap-drop ALL --read-only --tmpfs /tmp \
    --user 10001:10001 \
    --mount "type=bind,source=$SOURCE,target=/mnt/shared,bind-propagation=rslave" \
    --mount "type=bind,source=$HERE,target=/probe,readonly" \
    -e "PROBE_TAG=$TAG" \
    --entrypoint /probe/workspace-container.sh \
    "$IMAGE"
echo "container exit=$?"
