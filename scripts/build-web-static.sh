#!/usr/bin/env bash
# Build the SvelteKit frontend and copy it into the Python package as data, so
# `hatch build` ships the web UI in the wheel (works from a non-editable
# `uv tool install` with no repo checkout). Run this before `hatch build` /
# during release. Idempotent: the destination is wiped and repopulated.
#
# web_app._resolve_static_dir() falls back to src/istota/web_static when the
# repo-relative web/build is absent (installed package).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$REPO_ROOT/web"
BUILD_DIR="$WEB_DIR/build"
DEST_DIR="$REPO_ROOT/src/istota/web_static"

if [ ! -d "$WEB_DIR" ]; then
  echo "error: $WEB_DIR not found" >&2
  exit 1
fi

echo "==> npm install + build ($WEB_DIR)"
( cd "$WEB_DIR" && npm ci --no-audit --no-fund && npm run build )

if [ ! -d "$BUILD_DIR" ]; then
  echo "error: expected build output at $BUILD_DIR" >&2
  exit 1
fi

echo "==> copying $BUILD_DIR -> $DEST_DIR"
rm -rf "$DEST_DIR"
mkdir -p "$DEST_DIR"
cp -R "$BUILD_DIR/." "$DEST_DIR/"

echo "==> done. Packaged $(find "$DEST_DIR" -type f | wc -l | tr -d ' ') files into src/istota/web_static"
