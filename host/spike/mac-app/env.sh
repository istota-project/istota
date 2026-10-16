# mac-desktop-app Stage 1: shared settings for the probe scripts. Sourced.
#
# Everything runs in a scratch LIMA_HOME, never the default ~/.lima, whose
# `istota` and `istota-vm-1` instances are a real install and the vm tier's.
# Lima's unix sockets cap a path near 104 characters, so the root is short.
SPIKE_ROOT="${SPIKE_ROOT:-$HOME/.istota-spike}"
export LIMA_HOME="${SPIKE_ROOT}/lima"
ARTIFACTS="${SPIKE_ROOT}/artifacts"
SPIKE_WS="${SPIKE_ROOT}/ws"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../.." && pwd)"
TAG="${SPIKE_TAG:-v0.46.0}"
BUILD_VM=spike-build
APPLIANCE_VM=spike-appliance
SIGNED_VM=spike-signed

case "$LIMA_HOME" in
    "$HOME/.lima"|"$HOME/.lima/"*) echo "refusing the default LIMA_HOME" >&2; exit 64 ;;
esac
mkdir -p "$LIMA_HOME" "$ARTIFACTS" "$SPIKE_WS"

# Wall-clock seconds, for the timings.
now() { python3 -c 'import time; print(f"{time.time():.1f}")'; }
elapsed() { python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$1" "$(now)"; }
