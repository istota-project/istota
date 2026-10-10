#!/bin/bash
# The vm tier: the stack as provisioned on a dedicated VM, in a Lima VM.
#
# Boots (or reuses) the Lima instance istota-vmtier-1 from host/lima/istota.yaml,
# deploys a signed snapshot of this checkout through `istota-stack update`, and
# runs tests/vm/: parity rows 7, 9, 10, 11, 15, 18, 19 and 20, the halves of 12
# and 17 Docker Desktop cannot see, the full Nextcloud integration over the
# rclone mount, the browser under Rosetta, and the devbox runtime.
# tests/vm/conftest.py and tests/vm/lima.py say how.
#
#   scripts/test-vm.sh                    # the whole tier
#   scripts/test-vm.sh tests/vm/test_direct.py
#
# The first session creates the VM and builds every image in it (tens of
# minutes); later sessions reuse both. The VM is stopped at the end, unless
# ISTOTA_VM_KEEP_RUNNING=1. scripts/test-vm-negative-control.sh runs the
# controls. Needs limactl (Lima 2.x) on an Apple silicon Mac; skips without it.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$#" -eq 0 ]; then
    set -- tests/vm
fi
exec uv run pytest -m vm -n0 -p no:randomly -q --no-header -rfEs "$@"
