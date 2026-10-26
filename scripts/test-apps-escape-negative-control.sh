#!/bin/bash
# Prove the apps escape tier can see a frame with no CSP.
#
# The tier (web/tests-apps-escape/) asserts that an app frame makes no request
# after its own document. That assertion is only worth anything if it goes red
# when the isolation is gone, so this runs the hostile fixture with the frame
# served without its Content-Security-Policy header (APPS_ESCAPE_NO_CSP=1) and
# requires the hostile test to fail in every browser. **A clean run here is the
# failure**: it means the tier would pass on the bug it exists to catch.
#
# Only the hostile fixture is run. The contract fixture makes no request by
# design, so it stays green without a CSP and would prove nothing.
#
# Exit codes: 0 the control went red as required; 1 it did not; 2 no hostile
# fixture to run (tests/fixtures/apps/hostile/ needs manifest.toml and
# index.html; its README says what it must do).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="$ROOT/tests/fixtures/apps/hostile"

if [[ ! -f "$FIXTURE/manifest.toml" || ! -f "$FIXTURE/index.html" ]]; then
  echo "negative control: no hostile fixture at $FIXTURE (needs manifest.toml and index.html)" >&2
  exit 2
fi

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

set +e
APPS_ESCAPE_NO_CSP=1 npm --prefix "$ROOT/web" run test:apps-escape -- --grep 'app hostile ' >"$log" 2>&1
status=$?
set -e

missing=()
for browser in chromium firefox webkit; do
  if ! grep -Eq "^\s+[0-9]+\) \[$browser\] .*app hostile stays inside its frame" "$log"; then
    missing+=("$browser")
  fi
done

if [[ $status -ne 0 && ${#missing[@]} -eq 0 ]]; then
  echo "negative control: red in chromium, firefox and webkit without the CSP, as required"
  exit 0
fi

echo "negative control FAILED: the tier did not go red without the CSP" >&2
echo "  playwright exit: $status; browsers not reporting the hostile test failed: ${missing[*]:-none}" >&2
tail -40 "$log" >&2
exit 1
