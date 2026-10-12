#!/bin/bash
# local-vm-install Stage 1 (a): start latency-watch.py detached in the istota
# image under the run contract (see run-workspace.sh). Run as root in the guest.
#   start-watcher.sh <source-dir> [image]
set -euo pipefail
SOURCE="$1"
IMAGE="${2:-istota-test/lean:e74eebd3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker rm -f lat-watch >/dev/null 2>&1 || true
docker run -d --name lat-watch \
    --security-opt "seccomp=$HERE/seccomp-istota.json" \
    --security-opt apparmor=istota \
    --security-opt systempaths=unconfined \
    --security-opt no-new-privileges:true \
    --cap-drop ALL --read-only --tmpfs /tmp \
    --user 10001:10001 \
    --mount "type=bind,source=$SOURCE,target=/mnt/shared,bind-propagation=rslave" \
    --mount "type=bind,source=$HERE,target=/probe,readonly" \
    --entrypoint python3 \
    "$IMAGE" /probe/latency-watch.py
sleep 2
docker ps --filter name=lat-watch --format '{{.Status}}'
printf 'guest clock: %s\n' "$(date +%s.%N)"
