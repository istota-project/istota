#!/bin/bash
# mac-desktop-app Stage 1 (a): an appliance disk by hand.
#
#   build-appliance.sh create     # release-mode VM from host/lima/istota.yaml at $TAG
#   build-appliance.sh generalize # generalize.sh in the guest, then stop the VM
#   build-appliance.sh pack       # copy the disk out sparse, zstd it, record sizes
#
# The disk is the template's own release path (clone, verify tag, provision.sh),
# so it is the shape a user's VM has, not a second one.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

case "${1:-}" in
create)
    key="$(cat "${REPO}/host/release-signing-key.pub")"
    t0="$(now)"
    limactl create --tty=false --name="$BUILD_VM" \
        --param ISTOTA_TAG="$TAG" --param RELEASE_SIGNING_KEY="$key" \
        --param WORKSPACE="$SPIKE_WS" \
        --set 'del(.mounts[] | select(.mountPoint == "/mnt/istota-repo"))' \
        --set '.portForwards[0].guestPort = 18480 | .portForwards[0].hostPort = 18480' \
        --cpus 6 --memory 8 --disk 40 \
        "${REPO}/host/lima/istota.yaml"
    limactl start --tty=false --timeout 40m "$BUILD_VM"
    echo "create+start+provision: $(elapsed "$t0") s"
    limactl shell --workdir / "$BUILD_VM" sudo bash -c \
        'git -C /srv/app/istota/src describe --tags; ls -la /srv/app/istota; docker version --format "{{.Server.Version}}"; rclone version 2>/dev/null | head -1; df -h /'
    ;;
generalize)
    limactl copy "${HERE}/generalize.sh" "${BUILD_VM}:/tmp/generalize.sh"
    limactl shell --workdir / "$BUILD_VM" sudo bash /tmp/generalize.sh
    # The guest powers itself off; wait for Lima to see it, then make sure.
    for _ in $(seq 1 120); do
        [ "$(limactl list --format '{{.Status}}' "$BUILD_VM")" = Stopped ] && break
        sleep 2
    done
    limactl list "$BUILD_VM"
    [ "$(limactl list --format '{{.Status}}' "$BUILD_VM")" = Stopped ] || limactl stop "$BUILD_VM"
    ;;
pack)
    dir="${LIMA_HOME}/${BUILD_VM}"
    ls -ls "$dir"
    # Lima 2 keeps one raw `disk`; older ones had basedisk + diffdisk.
    for f in "$dir"/basedisk "$dir"/diffdisk "$dir"/disk; do
        [ -e "$f" ] && { printf '%s: ' "$f"; file -b "$f"; }
    done
    out="${ARTIFACTS}/istota-appliance-${TAG}.raw"
    # cp -c is an APFS clone: sparse and instant.
    rm -f "$out" "${out}.zst"
    cp -c "${dir}/disk" "$out"
    echo "apparent bytes: $(stat -f %z "$out")"
    echo "allocated KiB:  $(du -k "$out" | cut -f1)"
    t0="$(now)"
    zstd -q -T0 -19 --long=27 "$out" -o "${out}.zst"
    echo "zstd -19 --long=27 -T0: $(elapsed "$t0") s, $(stat -f %z "${out}.zst") bytes"
    t0="$(now)"
    zstd -q -d --long=27 "${out}.zst" -o "${out}.check" && rm -f "${out}.check"
    echo "unpack: $(elapsed "$t0") s"
    shasum -a 256 "$out" "${out}.zst"
    ;;
*)
    echo "usage: $0 create|generalize|pack" >&2; exit 64 ;;
esac
