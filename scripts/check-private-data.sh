#!/usr/bin/env bash
# Scan for private data that must never reach this public repo.
#
#   scripts/check-private-data.sh              # staged content (what the pre-commit hook runs)
#   scripts/check-private-data.sh --all        # every tracked file
#   scripts/check-private-data.sh FILE [FILE…] # named files, as they are on disk
#
# Patterns come from three places:
#   .private-data-patterns   committed, generic shapes only (cards, IBAN, key prefixes)
#   .private-data-local      gitignored, your own literals (names, hosts, account numbers)
#   derived at runtime       your home directory path and your git user.email
#
# Exit 0 = clean, 1 = private data found, 2 = the scan could not run.
#
# Matches are reported as file:line plus the pattern that fired. The matched
# text is never printed — a scanner that echoes the secret puts it in your
# scrollback, and from there into a chat log.
#
# Exempt a line that legitimately matches by putting `private-data-ok` on it.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

PATTERN_FILE=".private-data-patterns"
LOCAL_PATTERN_FILE=".private-data-local"
EXEMPT_MARKER="private-data-ok"

# Paths that are generated, vendored, or otherwise not ours to police.
SKIP_RE='^(node_modules/|web/node_modules/|\.venv/|dist/|\.ruff_cache/|\.mypy_cache/|\.pytest_cache/|web/build/|src/istota/web_static/)|(^|/)(uv\.lock|package-lock\.json)$'

# Terms too generic to denylist: matching one would flag most of the tree.
GENERIC_TERMS='^(user|users|admin|root|test|tests|dev|home|me|bot|alice|bob|carol|dana|localhost|ubuntu|debian|runner|build|ci|app|data|tmp|var)$'

# Documentation placeholders. A line carrying one of these is an example, not a
# value: "xxxxx-xxxxx-…", "glpat-xxxxxxxx", "<your-token>", "CHANGEME". Applied
# to the whole line, so it is a heuristic — gitleaks is the second net.
PLACEHOLDER_RE='(x{4,}|X{4,}|0{6,}|\*{4,}|CHANGE[_-]?ME|REPLACE[_-]?ME|YOUR[_-][A-Za-z]+|<[A-Za-z0-9_. -]+>)'

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

# --- collect patterns -------------------------------------------------------

patterns=()
pattern_labels=()

add_pattern() {  # add_pattern <regex> <label>
  [ -n "${1:-}" ] || return 0
  patterns+=("$1")
  pattern_labels+=("$2")
}

# Patterns are labelled by the comment heading above them, so a hit says which
# class of data fired without printing the value that fired it.
read_pattern_file() {  # read_pattern_file <path> <fallback-label>
  local file="$1" fallback="$2" line heading=""
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%$'\r'}"
    case "$line" in
      '') heading=""; continue ;;
      '#'*)
        line="${line###}"
        line="${line# }"
        # First short comment of a block is its heading; a blank line ends the
        # block, which is what keeps the file's prose preamble out of a label.
        [ -z "$heading" ] && [ "${#line}" -le 60 ] && heading="$line"
        continue
        ;;
    esac
    add_pattern "$line" "${heading:-$fallback}"
  done < "$file"
}

# Runtime-derived identity. Only values that are private by construction: an
# absolute path into someone's home directory, and a personal address. The git
# user *name* and the mail *domain* are deliberately not derived — this project
# puts both in LICENSE and README on purpose.
derive_identity_patterns() {
  local home_path email

  home_path="${HOME:-}"
  if [ -n "$home_path" ] && [ "$home_path" != "/" ]; then
    local base="${home_path##*/}"
    if [ "${#base}" -ge 3 ] && ! printf '%s' "$base" | grep -qiE "$GENERIC_TERMS"; then
      add_pattern "$(regex_escape "$home_path")" "local home directory path"
    fi
  fi

  email="$(git config --get user.email 2>/dev/null || true)"
  if [ -n "$email" ] && [ "${email#*@}" != "$email" ]; then
    add_pattern "$(regex_escape "$email")" "git user.email"
  fi
}

regex_escape() { printf '%s' "$1" | sed -E 's/[][\\.^$*+?(){}|\/]/\\&/g'; }

read_pattern_file "$PATTERN_FILE" "committed pattern"
read_pattern_file "$LOCAL_PATTERN_FILE" "local pattern"
derive_identity_patterns

if [ "${#patterns[@]}" -eq 0 ]; then
  echo "check-private-data: no patterns loaded ($PATTERN_FILE missing?)" >&2
  exit 2
fi

# --- collect targets --------------------------------------------------------

mode="staged"
files=()
case "${1:-}" in
  --all)    mode="all"; shift ;;
  --staged) mode="staged"; shift ;;
  -h|--help) usage; exit 0 ;;
  -*) echo "check-private-data: unknown option $1" >&2; exit 2 ;;
  ?*) mode="files" ;;
esac
[ "$mode" = "files" ] && files=("$@")

case "$mode" in
  staged) mapfile -t files < <(git diff --cached --name-only --diff-filter=ACMR) ;;
  all)    mapfile -t files < <(git ls-files) ;;
esac

# --- scan -------------------------------------------------------------------

# Materialize the target's content: the staged blob in staged mode, the worktree
# file otherwise. Writing to a file rather than a shell variable is what lets
# grep -I recognize (and skip) binary content instead of choking on null bytes.
tmp_content="$(mktemp)"
tmp_patterns="$(mktemp)"
trap 'rm -f "$tmp_content" "$tmp_patterns"' EXIT
printf '%s\n' "${patterns[@]}" > "$tmp_patterns"

found=0
scanned=0

for file in "${files[@]}"; do
  [ -n "$file" ] || continue
  printf '%s' "$file" | grep -qE "$SKIP_RE" && continue
  # Skip the pattern files themselves; a pattern is not an occurrence.
  case "$file" in
    "$PATTERN_FILE"|"$LOCAL_PATTERN_FILE"|"$LOCAL_PATTERN_FILE.example") continue ;;
  esac

  if [ "$mode" = "staged" ]; then
    git show ":$file" > "$tmp_content" 2>/dev/null || continue
  else
    [ -f "$file" ] || continue
    cat -- "$file" > "$tmp_content" 2>/dev/null || continue
  fi
  [ -s "$tmp_content" ] || continue
  scanned=$((scanned + 1))

  # Fast path: one pass over the file with every pattern at once. Only a file
  # that matches something pays for the per-pattern attribution loop below.
  grep -qIEf "$tmp_patterns" "$tmp_content" 2>/dev/null || continue

  for i in "${!patterns[@]}"; do
    hits="$(grep -nIE -- "${patterns[$i]}" "$tmp_content" 2>/dev/null \
      | grep -vF -- "$EXEMPT_MARKER" \
      | grep -vE -- "$PLACEHOLDER_RE" || true)"
    [ -n "$hits" ] || continue
    while IFS= read -r hit; do
      [ -n "$hit" ] || continue
      printf '  %s:%s  [%s]\n' "$file" "${hit%%:*}" "${pattern_labels[$i]}"
      found=$((found + 1))
    done <<< "$hits"
  done
done

if [ "$found" -gt 0 ]; then
  echo ""
  echo "  ERROR: private data detected in $found location(s)."
  echo "  The value itself is not printed — open the file:line above to see it."
  echo ""
  echo "  If the match is a false positive, put the marker '$EXEMPT_MARKER' on that line,"
  echo "  or narrow the pattern in $PATTERN_FILE / $LOCAL_PATTERN_FILE."
  echo "  See docs/development/secret-scanning.md"
  exit 1
fi

echo "check-private-data: clean (${scanned} file(s), ${#patterns[@]} pattern(s))"
exit 0
