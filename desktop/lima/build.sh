#!/bin/bash
# Build what the app bundles to run its VM, from the pinned sources in VERSION:
# limactl and the aarch64 guest agent from Lima's source, Lima's templates, and
# zstd. Everything goes under desktop/lima/build/ (gitignored):
#
#   build/bin/limactl, build/bin/zstd
#   build/share/lima/lima-guestagent.Linux-aarch64.gz, build/share/lima/templates/
#
# limactl looks for share/lima beside its own bin/, so the app copies both
# directories into Contents/Resources as they are. Nothing here is signed:
# desktop/build-app.sh signs each binary with its entitlements in the bundle.
#
# Needs Go (the version Lima's go.mod names, or newer), the Command Line Tools
# and curl. Run on an Apple silicon Mac.
#
#   desktop/lima/build.sh            # build what is missing
#   desktop/lima/build.sh --clean    # start again from the downloads
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/build"
SRC="${OUT}/src"

pinned() {
    local value
    value="$(sed -n "s/^$1=//p" "${HERE}/VERSION")"
    [ -n "$value" ] || { echo "build.sh: VERSION has no $1" >&2; exit 1; }
    printf '%s\n' "$value"
}

LIMA_VERSION="$(pinned LIMA_VERSION)"
ZSTD_VERSION="$(pinned ZSTD_VERSION)"
TARGET="$(pinned MACOSX_DEPLOYMENT_TARGET)"

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] \
    || { echo "build.sh: builds on an Apple silicon Mac only" >&2; exit 1; }
command -v go >/dev/null || { echo "build.sh: needs Go (brew install go)" >&2; exit 1; }

if [ "${1:-}" = --clean ]; then
    rm -rf "$OUT"
fi
mkdir -p "$SRC" "${OUT}/bin" "${OUT}/share/lima"

# fetch NAME: the tarball VERSION pins, checked, unpacked under src/ once.
fetch() {
    local name="$1" url sha tarball actual
    url="$(pinned "${name}_SOURCE_URL")"
    sha="$(pinned "${name}_SOURCE_SHA256")"
    tarball="${SRC}/$(basename "$url")"
    if [ ! -f "$tarball" ]; then
        curl -fsSL --retry 3 -o "${tarball}.part" "$url"
        mv "${tarball}.part" "$tarball"
    fi
    actual="$(shasum -a 256 "$tarball" | cut -d' ' -f1)"
    if [ "$actual" != "$sha" ]; then
        echo "build.sh: ${tarball} has SHA-256 ${actual}, VERSION pins ${sha}" >&2
        rm -f "$tarball"
        exit 1
    fi
    tar -C "$SRC" -xzf "$tarball"
}

# minos BINARY: the macOS version a Mach-O declares as its minimum.
minos() {
    otool -l "$1" | awk '/LC_BUILD_VERSION/ { f = 1 } f && $1 == "minos" { print $2; exit }'
}

export MACOSX_DEPLOYMENT_TARGET="$TARGET"
export CGO_CFLAGS="-mmacosx-version-min=${TARGET}" CGO_LDFLAGS="-mmacosx-version-min=${TARGET}"
export GOTOOLCHAIN=local GOFLAGS=-trimpath

if [ ! -x "${OUT}/bin/limactl" ] || [ ! -f "${OUT}/share/lima/lima-guestagent.Linux-aarch64.gz" ]; then
    fetch LIMA
    lima="${SRC}/lima-${LIMA_VERSION}"
    ldflags="-s -w -X github.com/lima-vm/lima/v2/pkg/version.Version=${LIMA_VERSION}"
    # Not Lima's Makefile: it stamps the version from git, and this is a tarball.
    # CGO for limactl, so the host agent's DNS uses the system resolver.
    (cd "$lima" && CGO_ENABLED=1 GOOS=darwin GOARCH=arm64 \
        go build -ldflags "$ldflags" -o "${OUT}/bin/limactl" ./cmd/limactl)
    (cd "$lima" && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
        go build -ldflags "$ldflags" -o "${OUT}/share/lima/lima-guestagent.Linux-aarch64" ./cmd/lima-guestagent)
    gzip -n -f "${OUT}/share/lima/lima-guestagent.Linux-aarch64"
    rm -rf "${OUT}/share/lima/templates"
    cp -R "${lima}/templates" "${OUT}/share/lima/templates"
fi

if [ ! -x "${OUT}/bin/zstd" ]; then
    fetch ZSTD
    make -s -C "${SRC}/zstd-${ZSTD_VERSION}/programs" zstd-release \
        MOREFLAGS="-mmacosx-version-min=${TARGET}" HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 >/dev/null
    cp "${SRC}/zstd-${ZSTD_VERSION}/programs/zstd" "${OUT}/bin/zstd"
fi

for binary in "${OUT}/bin/limactl" "${OUT}/bin/zstd"; do
    declared="$(minos "$binary")"
    if [ "$declared" != "$TARGET" ]; then
        echo "build.sh: ${binary} declares macOS ${declared:-nothing}, not ${TARGET}" >&2
        exit 1
    fi
    # Nothing outside the system: the app runs on a Mac with no Homebrew.
    if otool -L "$binary" | tail -n +2 | grep -v -E '^\s+/(usr/lib|System/Library)/' >/dev/null; then
        echo "build.sh: ${binary} links a library outside the system:" >&2
        otool -L "$binary" >&2
        exit 1
    fi
done

echo "limactl $("${OUT}/bin/limactl" --version | awk '{print $3}'), zstd ${ZSTD_VERSION}, macOS ${TARGET} and later: ${OUT}"
