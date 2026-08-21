#!/usr/bin/env bash
# Copy the stdlib-only leaves the devbox image needs into its build context.
#
# Docker cannot COPY from outside its build context, and the devbox context is
# docker/devbox/. Moving the context to the repo root would pull the whole tree
# into every build, and a symlink pointing out of the context fails the same way
# a path would. So: one canonical file under src/, one generated copy here, and
# a test that fails when they drift.
#
# Run this after editing any file listed below, then commit both copies.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest_dir="${repo_root}/docker/devbox/lib"

# source-relative-path  ->  destination basename
sync_pairs=(
    "src/istota/forge_cli.py:istota_forge_cli.py"
)

mkdir -p "${dest_dir}"

changed=0
for pair in "${sync_pairs[@]}"; do
    src="${repo_root}/${pair%%:*}"
    dest="${dest_dir}/${pair##*:}"
    if [ ! -f "${src}" ]; then
        echo "sync-devbox-lib: missing source ${src}" >&2
        exit 1
    fi
    # A symlink here is the one failure this whole script exists to prevent:
    # every check below follows it and reports "in sync", while `docker build`
    # fails with "COPY failed: ... outside the build context".
    if [ -L "${dest}" ]; then
        echo "sync-devbox-lib: ${dest} is a symlink; it must be a real copy" >&2
        exit 1
    fi
    if [ -f "${dest}" ] && cmp -s "${src}" "${dest}"; then
        continue
    fi
    cp "${src}" "${dest}"
    echo "synced ${pair%%:*} -> docker/devbox/lib/${pair##*:}"
    changed=1
done

if [ "${changed}" -eq 0 ]; then
    echo "sync-devbox-lib: already in sync"
fi
