#!/usr/bin/env bash
# Copy the stdlib-only leaves the devbox and browser images need into their build contexts.
#
# Docker cannot COPY from outside its build context, and these contexts are
# docker/devbox/ and docker/browser/. Moving the context to the repo root would pull the whole tree
# into every build, and a symlink pointing out of the context fails the same way
# a path would. So: one canonical file under src/, one generated copy here, and
# a test that fails when they drift.
#
# Run this after editing any file listed below, then commit both copies.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# source-relative-path  ->  repo-relative destination
#
# Byte copies only. docker/devbox/lib/istota_devbox_client.py is deliberately
# NOT here: it is a rewrite of devbox_proxy_protocol's client half for a shim
# with no istota package, and it carries `call`, `die` and two exception classes
# that file has no reason to hold — adding it would overwrite the rewrite and
# break git-credential-istota's import at image build. It is pinned
# behaviourally instead, in tests/test_devbox_vendored_lib.py, which also lists
# these directories and fails on any file pinned by neither mechanism.
sync_pairs=(
    "src/istota/forge_cli.py:docker/devbox/lib/istota_forge_cli.py"
    "src/istota/devbox_exec_protocol.py:docker/devbox/lib/istota_devbox_exec_protocol.py"
    "src/istota/user_scope.py:docker/browser/lib/istota_user_scope.py"
)

changed=0
for pair in "${sync_pairs[@]}"; do
    src="${repo_root}/${pair%%:*}"
    dest="${repo_root}/${pair##*:}"
    mkdir -p "$(dirname "${dest}")"
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
    echo "synced ${pair%%:*} -> ${pair##*:}"
    changed=1
done

if [ "${changed}" -eq 0 ]; then
    echo "sync-devbox-lib: already in sync"
fi
