#!/bin/bash
# Sync emissaries.md from your canonical upstream copy.
#
# There is no default upstream — set EMISSARIES_URL to the raw URL of the
# emissaries.md you want to track (an env var, or edit the fallback below).
# The script refuses to run without one rather than quietly leaving the local
# config/emissaries.md untouched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TARGET="$PROJECT_DIR/config/emissaries.md"
URL="${EMISSARIES_URL:-}"

if [ -z "$URL" ]; then
    echo "error: EMISSARIES_URL is not set." >&2
    echo "       Point it at the raw URL of your emissaries.md, e.g." >&2
    echo "       EMISSARIES_URL=https://example.com/emissaries.md $0" >&2
    exit 1
fi

echo "Fetching latest emissaries.md from $URL..."
curl -fsSL "$URL" -o "$TARGET"
echo "Updated $TARGET"
