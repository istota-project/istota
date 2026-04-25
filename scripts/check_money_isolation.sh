#!/usr/bin/env bash
# Fail if src/money/ contains any imports from istota.*
#
# Run as part of CI on release tags so an accidental cross-package import
# can't slip into a public extract.
set -euo pipefail

cd "$(dirname "$0")/.."

if grep -rEn '^(\s*from\s+istota|\s*import\s+istota)' src/money/ ; then
    echo
    echo "ERROR: src/money/ must not import from istota.*" >&2
    echo "       The public moneyman extract is built from this tree and must" >&2
    echo "       run standalone." >&2
    exit 1
fi

echo "OK: src/money/ has no istota.* imports."
