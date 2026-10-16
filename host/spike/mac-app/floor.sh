#!/bin/bash
# mac-desktop-app Stage 1 (g): the macOS floor, read from the SDK headers and
# from the minos each Lima release's limactl declares (LC_BUILD_VERSION).
#
#   floor.sh [lima-version ...]     # default: 2.0.0 2.1.0 2.2.1
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

SDK="$(xcrun --sdk macosx --show-sdk-path)"
echo "== SDK availability ($SDK)"
grep -h "dataStoreForIdentifier" "$SDK/System/Library/Frameworks/WebKit.framework/Headers/WKWebsiteDataStore.h"
VZ="$SDK/System/Library/Frameworks/Virtualization.framework/Headers"
for h in VZVirtioFileSystemDeviceConfiguration VZLinuxRosettaDirectoryShare VZEFIBootLoader VZNATNetworkDeviceAttachment; do
    printf '%s: %s\n' "$h" "$(grep -m1 -o 'API_AVAILABLE(macos([0-9.]*))' "$VZ/$h.h")"
done

echo "== limactl minos per release"
for v in "${@:-2.0.0 2.1.0 2.2.1}"; do
    for one in $v; do
        dir="${SPIKE_ROOT}/dl/lima-floor/${one}"
        mkdir -p "$dir"
        if [ ! -f "${dir}/bin/limactl" ]; then
            curl -fsSL "https://github.com/lima-vm/lima/releases/download/v${one}/lima-${one}-Darwin-arm64.tar.gz" \
                | tar -C "$dir" -xzf - bin/limactl
        fi
        printf '%s: %s\n' "$one" "$(otool -l "${dir}/bin/limactl" | awk '/LC_BUILD_VERSION/ { f = 1 } f && /minos/ { print $2; exit }')"
    done
done
printf 'homebrew %s: %s\n' "$(limactl --version | awk '{print $3}')" \
    "$(otool -l "$(command -v limactl)" | awk '/LC_BUILD_VERSION/ { f = 1 } f && /minos/ { print $2; exit }')"
