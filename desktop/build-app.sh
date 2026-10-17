#!/bin/bash
# Build Istota.app from this checkout and sign it inside out.
#
#   desktop/build-app.sh [--identity ID] [--features LIST] [--target-dir DIR] [--release DIR]
#
# The bundle holds the Tauri shell and its UI, the Mac CLI and the Lima
# template (tauri.conf.json's resources), and what desktop/lima/build.sh builds:
# limactl, the guest agent, Lima's templates and zstd, all under
# Contents/Resources so the CLI finds them on PATH and limactl finds share/lima
# beside its bin/. Release artifacts (the appliance disk, the images) are not
# in it; scripts/release-mac.sh adds them. --release DIR copies one release
# directory (scripts/build-appliance.sh's <out>/<tag>/) into
# Contents/Resources/release/<tag>/ as APFS clones before signing; the
# desktop tier's first run uses it.
#
# ID is a codesign identity, a SHA-1 or a name; the default "-" signs ad hoc.
# The Apple Development identity signs a build for this Mac only; Developer ID
# and notarization are scripts/release-mac.sh's. limactl is signed with
# entitlements/limactl.plist (Virtualization.framework), everything with the
# hardened runtime. The last line printed is the app's path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
identity="-"
features=""
target_dir="${HERE}/src-tauri/target"
release=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --identity) identity="$2"; shift 2 ;;
        --features) features="$2"; shift 2 ;;
        --target-dir) target_dir="$2"; shift 2 ;;
        --release) release="$2"; shift 2 ;;
        *) echo "usage: $0 [--identity ID] [--features LIST] [--target-dir DIR] [--release DIR]" >&2; exit 64 ;;
    esac
done

"${HERE}/lima/build.sh"
[ -d "${HERE}/node_modules" ] || npm --prefix "$HERE" ci

build=(build --bundles app --no-sign)
[ -z "$features" ] || build+=(--features "$features")
(cd "$HERE" && CARGO_TARGET_DIR="$target_dir" npx tauri "${build[@]}")

app="${target_dir}/release/bundle/macos/Istota.app"
resources="${app}/Contents/Resources"
[ -d "$resources" ] || { echo "build-app.sh: no ${app}" >&2; exit 1; }

mkdir -p "${resources}/bin" "${resources}/share"
cp "${HERE}/lima/build/bin/limactl" "${HERE}/lima/build/bin/zstd" "${resources}/bin/"
rm -rf "${resources}/share/lima"
cp -R "${HERE}/lima/build/share/lima" "${resources}/share/lima"
chmod 0755 "${resources}/bin/istota-stack" "${resources}/bin/istota"
rm -rf "${resources}/release"
if [ -n "$release" ]; then
    tag="$(basename "$release")"
    [ -f "${release}/istota-appliance-${tag}.raw.zst" ] \
        || { echo "build-app.sh: ${release} holds no istota-appliance-${tag}.raw.zst" >&2; exit 1; }
    mkdir -p "${resources}/release"
    cp -c -R "$release" "${resources}/release/${tag}"
fi

sign() {
    codesign --force --timestamp=none --options runtime --sign "$identity" "$@"
}
sign --entitlements "${HERE}/src-tauri/entitlements/limactl.plist" "${resources}/bin/limactl"
sign "${resources}/bin/zstd"
sign --entitlements "${HERE}/src-tauri/entitlements/app.plist" "$app"
codesign --verify --deep --strict "$app"

echo "Istota.app: $(/usr/bin/du -sh "$app" | cut -f1), signed by ${identity}"
echo "$app"
