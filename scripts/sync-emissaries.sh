#!/bin/bash
# Sync emissaries.md from the canonical public repo
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TARGET="$PROJECT_DIR/config/emissaries.md"
URL="https://forge.example.com/alice/emissaries/raw/branch/main/emissaries.md"

echo "Fetching latest emissaries.md..."
curl -fsSL "$URL" -o "$TARGET"
echo "Updated $TARGET"
