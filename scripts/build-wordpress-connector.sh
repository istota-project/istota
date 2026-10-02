#!/usr/bin/env bash
# Build the istota-connector WordPress plugin into an installable zip.
#
#   scripts/build-wordpress-connector.sh [REF]
#
# The zip holds integrations/wordpress/istota-connector/ as committed at REF
# (default HEAD), under one istota-connector/ directory, which is the layout
# wp-admin's "Upload Plugin" expects. It is built with `git archive`, so
# uncommitted edits and stray files never end up on a site. Written to
# dist/istota-connector-<version>.zip, the version read from the plugin header.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="${1:-HEAD}"
PLUGIN_PATH="integrations/wordpress/istota-connector"

cd "$REPO_ROOT"
if ! git cat-file -e "$REF:$PLUGIN_PATH/istota-connector.php" 2>/dev/null; then
  echo "error: $PLUGIN_PATH/istota-connector.php is not committed at $REF" >&2
  exit 1
fi

VERSION="$(git show "$REF:$PLUGIN_PATH/istota-connector.php" \
  | sed -n 's/^ \* Version: *\([0-9A-Za-z.-]*\).*/\1/p' | head -n 1)"
if [ -z "$VERSION" ]; then
  echo "error: no Version line in the plugin header" >&2
  exit 1
fi

mkdir -p dist
OUT="dist/istota-connector-$VERSION.zip"
git archive --format=zip --prefix=istota-connector/ -o "$OUT" "$REF:$PLUGIN_PATH"
echo "==> wrote $OUT"
