#!/bin/bash
# The desktop tier: Istota.app built from this checkout and driven on this Mac.
#
#   scripts/test-desktop.sh                  # build the app, then run tests/desktop
#   scripts/test-desktop.sh tests/desktop/test_confinement.py
#   scripts/test-desktop.sh --control m9     # row M9's negative control: must go red
#
# The build is desktop/build-app.sh, signed with ISTOTA_DESKTOP_IDENTITY (a
# codesign identity; ad hoc when unset). ISTOTA_DESKTOP_APP names an app to
# test instead of building one. Needs an Apple silicon Mac with Go, Rust and
# Node (desktop/README.md); the first build fetches Lima's and zstd's sources
# and the Rust crates. Row M9 follows a link to another origin, which the app
# opens in the default browser: expect a browser tab.
#
# The control builds the app with the m9-negative-control feature into a
# target directory of its own and passes only when the M9 witness turns red.
set -euo pipefail

cd "$(dirname "$0")/.."

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] \
    || { echo "test-desktop.sh: the desktop tier runs on an Apple silicon Mac" >&2; exit 1; }

identity="${ISTOTA_DESKTOP_IDENTITY:--}"

if [ "${1:-}" = --control ]; then
    [ "${2:-}" = m9 ] || { echo "test-desktop.sh: the controls are: m9" >&2; exit 64; }
    target="desktop/src-tauri/target/m9-control"
    app="$(desktop/build-app.sh --identity "$identity" --features m9-negative-control --target-dir "$PWD/$target" | tail -n 1)"
    witness="tests/desktop/test_confinement.py::TestWebUIContentCannotCallIntoTheApp::test_the_page_sees_no_tauri_ipc"
    out="$(mktemp)"
    trap 'rm -f "$out"' EXIT
    set +e
    ISTOTA_DESKTOP_APP="$app" uv run pytest -m desktop -n0 -p no:randomly -q --no-header -rfE "$witness" >"$out" 2>&1
    rc=$?
    set -e
    cat "$out"
    if [ "$rc" -ne 0 ] && grep -q "^FAILED ${witness}" "$out"; then
        echo "control m9: red, as it must be (the probe saw Tauri's IPC in a stock Tauri webview)"
        exit 0
    fi
    echo "control m9: the witness did not go red (exit ${rc}); it cannot tell a confined window from an open one" >&2
    exit 1
fi

if [ -z "${ISTOTA_DESKTOP_APP:-}" ]; then
    ISTOTA_DESKTOP_APP="$(desktop/build-app.sh --identity "$identity" | tail -n 1)"
    export ISTOTA_DESKTOP_APP
fi
if [ "$#" -eq 0 ]; then
    set -- tests/desktop
fi
exec uv run pytest -m desktop -n0 -p no:randomly -q --no-header -rfEs "$@"
