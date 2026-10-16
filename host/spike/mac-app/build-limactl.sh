#!/bin/bash
# mac-desktop-app Stage 1 (g)/(d): Lima's own 2.x release binaries declare
# minos 26.0, so the app would need macOS 26. Does limactl build from source
# with an older deployment target, and what does it then declare?
#
#   build-limactl.sh [deployment-target]     # default 14.0
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

TARGET="${1:-14.0}"
LIMA_VERSION=2.2.1
GO_VERSION=1.25.3
work="${SPIKE_ROOT}/dl/limasrc"
mkdir -p "$work"

if [ ! -x "${work}/go/bin/go" ]; then
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.darwin-arm64.tar.gz" | tar -C "$work" -xzf -
fi
if [ ! -d "${work}/lima-${LIMA_VERSION}" ]; then
    curl -fsSL "https://github.com/lima-vm/lima/archive/refs/tags/v${LIMA_VERSION}.tar.gz" | tar -C "$work" -xzf -
fi
export PATH="${work}/go/bin:$PATH" GOTOOLCHAIN=auto GOPATH="${work}/gopath" GOCACHE="${work}/gocache"
export MACOSX_DEPLOYMENT_TARGET="$TARGET"
export CGO_CFLAGS="-mmacosx-version-min=${TARGET}" CGO_LDFLAGS="-mmacosx-version-min=${TARGET}"
cd "${work}/lima-${LIMA_VERSION}"
t0="$(now)"
# Not `make`: it stamps the version from git, and this is a tarball.
go build -ldflags "-s -w -X github.com/lima-vm/lima/v2/pkg/version.Version=${LIMA_VERSION}" \
    -o "${work}/limactl-${TARGET}" ./cmd/limactl
echo "build: $(elapsed "$t0") s"
otool -l "${work}/limactl-${TARGET}" | awk '/LC_BUILD_VERSION/ { f = 1 } f && /minos|sdk/ { print } f && /sdk/ { exit }'
"${work}/limactl-${TARGET}" --version
