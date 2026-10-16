#!/bin/bash
# mac-desktop-app Stage 1 (d): a minimal Istota.app holding a hardened-runtime
# limactl with com.apple.security.virtualization, started from the bundle with
# a private LIMA_HOME, boots a vz VM with Rosetta.
#
#   SIGN_IDENTITY=<sha1 or "-"> signed-app.sh build LIMACTL [GUESTAGENT_DIR]
#   signed-app.sh boot          # the appliance VM through the bundle's limactl
#   signed-app.sh rosetta       # an amd64 container in it
#
# SIGN_IDENTITY "-" is ad hoc; the stage used the maintainer's Apple
# Development certificate. No Developer ID, no notarization here.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

APP="${ARTIFACTS}/Istota.app"
PRIVATE_LIMA_HOME="${SPIKE_ROOT}/applima"
BUNDLED="${APP}/Contents/Resources/lima/bin/limactl"

case "${1:-}" in
build)
    limactl_src="$2"
    agents="${3:-${SPIKE_ROOT}/dl/lima/x/share/lima}"
    identity="${SIGN_IDENTITY:--}"
    rm -rf "$APP"
    mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources/lima/bin" "${APP}/Contents/Resources/lima/share/lima"
    cp "$limactl_src" "$BUNDLED"
    cp "${agents}/lima-guestagent.Linux-aarch64.gz" "${APP}/Contents/Resources/lima/share/lima/"
    # The app's own executable: runs the bundled limactl with the app's LIMA_HOME.
    cat > "${ARTIFACTS}/stub.c" <<'C'
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char self[PATH_MAX], real[PATH_MAX], tool[PATH_MAX];
    uint32_t size = sizeof self;
    if (_NSGetExecutablePath(self, &size) != 0 || !realpath(self, real)) return 70;
    snprintf(tool, sizeof tool, "%s/../Resources/lima/bin/limactl", dirname(real));
    const char *home = getenv("ISTOTA_APP_LIMA_HOME");
    if (!home) return 64;
    setenv("LIMA_HOME", home, 1);
    argv[0] = tool;
    execv(tool, argv);
    perror("execv");
    return 71;
}
C
    xcrun clang -O2 -mmacosx-version-min=14.0 -o "${APP}/Contents/MacOS/Istota" "${ARTIFACTS}/stub.c"
    cat > "${APP}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>org.istota.spike</string>
  <key>CFBundleName</key><string>Istota</string>
  <key>CFBundleExecutable</key><string>Istota</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.0.1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
    cat > "${ARTIFACTS}/limactl.entitlements" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.virtualization</key><true/>
</dict></plist>
PLIST
    # Inside out: the nested tool first, with its entitlement, then the bundle.
    codesign --force --timestamp=none --options runtime \
        --entitlements "${ARTIFACTS}/limactl.entitlements" --sign "$identity" "$BUNDLED"
    codesign --force --timestamp=none --options runtime --sign "$identity" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    codesign -d --entitlements - --verbose=2 "$BUNDLED" 2>&1 | grep -E 'Authority|flags|TeamIdentifier|virtualization|Format'
    spctl -a -t exec -vv "$APP" 2>&1 || echo "spctl: rejected (expected without Developer ID and notarization)"
    otool -l "$BUNDLED" | awk '/LC_BUILD_VERSION/ { f = 1 } f && /minos/ { print "limactl minos " $2; exit }'
    /usr/bin/du -sk "$APP"
    ;;
boot)
    export ISTOTA_APP_LIMA_HOME="$PRIVATE_LIMA_HOME"
    mkdir -p "$PRIVATE_LIMA_HOME"
    image="${ARTIFACTS}/istota-appliance-${TAG}.raw"
    digest="sha256:$(shasum -a 256 "$image" | cut -d' ' -f1)"
    yaml="${ARTIFACTS}/${SIGNED_VM}.yaml"
    sed -e "s#@IMAGE@#${image}#" -e "s#@DIGEST@#${digest}#" -e 's#18481#18482#g' \
        "${HERE}/appliance.yaml.in" > "$yaml"
    t0="$(now)"
    "${APP}/Contents/MacOS/Istota" create --tty=false --name="$SIGNED_VM" \
        --param RELEASE_SIGNING_KEY="$(cat "${REPO}/host/release-signing-key.pub")" \
        --param WORKSPACE="$SPIKE_WS" --param OFFLINE=0 --param OFFLINE_RULE=1 "$yaml"
    "${APP}/Contents/MacOS/Istota" start --tty=false --timeout 15m "$SIGNED_VM"
    echo "create+start through the bundle: $(elapsed "$t0") s"
    "${APP}/Contents/MacOS/Istota" list
    # The VM's process is the bundle's limactl, not Homebrew's.
    # The stub's path is not normalized, so it reads MacOS/../Resources.
    ps -o command= -p "$(cat "${PRIVATE_LIMA_HOME}/${SIGNED_VM}/ha.pid")" | cut -c1-160
    ;;
rosetta)
    export ISTOTA_APP_LIMA_HOME="$PRIVATE_LIMA_HOME"
    "${APP}/Contents/MacOS/Istota" shell --workdir / "$SIGNED_VM" sudo bash -c '
        echo "binfmt: $(cat /proc/sys/fs/binfmt_misc/rosetta 2>/dev/null | head -2 | tr "\n" " ")"
        findmnt -no SOURCE,FSTYPE /mnt/lima-rosetta
        # A Mac whose outbound filter holds the new binary (Little Snitch here)
        # gets the image carried in: `docker save --platform linux/amd64` elsewhere.
        [ -f /tmp/alpine-amd64.tar ] && docker load -q -i /tmp/alpine-amd64.tar
        docker run --rm --network none --platform linux/amd64 alpine:3.20 sh -c "uname -m; head -c 4 /bin/busybox | od -An -c"'
    ;;
*) echo "usage: $0 build LIMACTL [AGENTS]|boot|rosetta" >&2; exit 64 ;;
esac
