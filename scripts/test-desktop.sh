#!/bin/bash
# The desktop tier: Istota.app built from this checkout and driven on this Mac.
#
#   scripts/test-desktop.sh                  # build the app, then run tests/desktop
#   scripts/test-desktop.sh tests/desktop/test_confinement.py
#   scripts/test-desktop.sh --control m9     # row M9's negative control: must go red
#   scripts/test-desktop.sh --control m6     # row M6's negative control: must go red
#
# The build is desktop/build-app.sh, signed with ISTOTA_DESKTOP_IDENTITY (a
# codesign identity; ad hoc when unset), with the desktop-test-hooks feature
# and into a target directory of its own, so no tier build is mistaken for a
# release. ISTOTA_DESKTOP_APP names an app to test instead of building one.
# ISTOTA_DESKTOP_RELEASE names a release directory from
# scripts/build-appliance.sh (<out>/<tag>/) to bundle, which the first-run
# tests need; ISTOTA_DESKTOP_RELEASE_KEY is the public key its tag and
# manifest are signed with (the project's when unset). Needs an Apple silicon
# Mac with Go, Rust and Node (desktop/README.md); the first build fetches
# Lima's and zstd's sources and the Rust crates. The app's window appears
# while a test runs; links it would open in the default browser go to a
# recording opener instead.
#
# Each control builds its feature into a target directory of its own and
# passes only when its witness turns red.
set -euo pipefail

cd "$(dirname "$0")/.."

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] \
    || { echo "test-desktop.sh: the desktop tier runs on an Apple silicon Mac" >&2; exit 1; }

identity="${ISTOTA_DESKTOP_IDENTITY:--}"
release_args=()
[ -z "${ISTOTA_DESKTOP_RELEASE:-}" ] || release_args=(--release "$ISTOTA_DESKTOP_RELEASE")

build() {
    local features="$1" target="$2"
    desktop/build-app.sh --identity "$identity" --features "$features" --target-dir "$PWD/$target" \
        ${release_args[@]+"${release_args[@]}"} | tail -n 1
}

if [ "${1:-}" = --control ]; then
    case "${2:-}" in
        m9)
            app="$(build m9-negative-control desktop/src-tauri/target/m9-control)"
            witness="tests/desktop/test_confinement.py::TestWebUIContentCannotCallIntoTheApp::test_the_page_sees_no_tauri_ipc"
            red="the probe saw Tauri's IPC in a stock Tauri webview"
            ;;
        m6)
            [ -n "${ISTOTA_DESKTOP_RELEASE:-}" ] \
                || { echo "test-desktop.sh: --control m6 needs ISTOTA_DESKTOP_RELEASE for its first run" >&2; exit 64; }
            app="$(build m6-negative-control desktop/src-tauri/target/m6-control)"
            witness="tests/desktop/test_first_run.py::TestKeysStayOffTheMac::test_no_argv_held_a_key"
            red="the sampler found the master key on security's argv"
            ;;
        *) echo "test-desktop.sh: the controls are: m6, m9" >&2; exit 64 ;;
    esac
    out="$(mktemp)"
    trap 'rm -f "$out"' EXIT
    set +e
    ISTOTA_DESKTOP_APP="$app" uv run pytest -m desktop -n0 -p no:randomly -q --no-header -rfE "$witness" >"$out" 2>&1
    rc=$?
    set -e
    cat "$out"
    if [ "$rc" -ne 0 ] && grep -q "^FAILED ${witness}" "$out"; then
        echo "control $2: red, as it must be (${red})"
        exit 0
    fi
    echo "control $2: the witness did not go red (exit ${rc}); it cannot tell a leak from none" >&2
    exit 1
fi

if [ -z "${ISTOTA_DESKTOP_APP:-}" ]; then
    ISTOTA_DESKTOP_APP="$(build desktop-test-hooks desktop/src-tauri/target/desktop-tier)"
    export ISTOTA_DESKTOP_APP
fi
if [ "$#" -eq 0 ]; then
    set -- tests/desktop
fi
exec uv run pytest -m desktop -n0 -p no:randomly -q --no-header -rfEs "$@"
