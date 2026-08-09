#!/usr/bin/env bash
#
# Single verification entry point: lint, types, tests. Prints failures only.
# A green run is silent, so the Stop hook that calls it costs nothing.
#
#   .claude/verify.sh          auto — run the halves that have uncommitted changes
#   .claude/verify.sh --all    both halves regardless
#   .claude/verify.sh --py     python only
#   .claude/verify.sh --web    web only
#
# Exits non-zero if any check failed. Every check runs even after one fails,
# so a single invocation reports everything that is broken.

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

failed=0

# Run a check, swallowing its output unless it fails.
run() {
  local label="$1"
  shift
  local out
  if ! out="$("$@" 2>&1)"; then
    printf '=== FAIL: %s ===\n%s\n\n' "$label" "$out"
    failed=1
  fi
}

# rtk trims passing output to nothing; without it, fall back to the raw runner.
if command -v rtk >/dev/null 2>&1; then
  filter=(rtk test)
else
  filter=()
fi

scope="auto"
case "${1:-}" in
  --all) scope="all" ;;
  --py | --python) scope="py" ;;
  --web) scope="web" ;;
  "") ;;
  *)
    echo "verify.sh: unknown argument '$1'" >&2
    exit 2
    ;;
esac

do_py=0
do_web=0
case "$scope" in
  all) do_py=1; do_web=1 ;;
  py) do_py=1 ;;
  web) do_web=1 ;;
  auto)
    # Scope to whichever half is dirty. Nothing dirty (or no git) => run both.
    dirty="$(git status --porcelain 2>/dev/null)"
    if [ -z "$dirty" ]; then
      do_py=1
      do_web=1
    else
      # Field 2+ of a porcelain line is the path; a rename prints "old -> new".
      paths="$(printf '%s\n' "$dirty" | cut -c4- | sed 's/.* -> //')"
      printf '%s\n' "$paths" | grep -q '^web/' && do_web=1
      printf '%s\n' "$paths" | grep -qvE '^web/' && do_py=1
    fi
    ;;
esac

if [ "$do_py" = 1 ]; then
  # Rules and per-file ignores live in [tool.ruff.lint] in pyproject.toml.
  # No formatter check: `ruff format` is not adopted, and the hand formatting
  # in the tree is the baseline — see the comment on the config block.
  run "ruff" ruff check --output-format concise src tests

  # pyproject pins `-m 'not integration and not live' -n auto`.
  run "pytest" ${filter[@]+"${filter[@]}"} uv run pytest
fi

if [ "$do_web" = 1 ]; then
  if [ ! -d web/node_modules ]; then
    echo "verify.sh: skipping web checks — run 'npm ci' in web/" >&2
  else
    run "design lint" npm --prefix web run lint:design
    run "svelte-check" npm --prefix web run check
    run "vitest" ${filter[@]+"${filter[@]}"} npm --prefix web run test
    run "prettier" npm --prefix web run format:check
  fi
fi

exit "$failed"
