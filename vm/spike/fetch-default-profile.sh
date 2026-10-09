#!/bin/bash
# Fetch the seccomp profile Docker Engine compiles in, at the engine version
# the VM runs, so the draft profile is derived from the exact default it
# replaces. Usage: fetch-default-profile.sh <engine-version> <out-file>
set -euo pipefail
version="$1"
out="$2"
# Engine releases since 29 are tagged `docker-vX.Y.Z`; older ones `vX.Y.Z`.
# The profile moved to the moby/profiles module and is vendored from there.
for ref in "docker-v${version}/vendor/github.com/moby/profiles" "v${version}/profiles"; do
    url="https://raw.githubusercontent.com/moby/moby/${ref}/seccomp/default.json"
    if curl -fsSL -o "$out" "$url"; then
        echo "fetched $url"
        exit 0
    fi
done
echo "no default profile found for engine ${version}" >&2
exit 1
