#!/usr/bin/env bash
# Cut a new release: move CHANGELOG [Unreleased] to a versioned section,
# bump pyproject.toml, commit, tag with the section as the annotation body,
# push to origin. The GitHub mirror's release.yml workflow creates the
# GitHub Release from the tag annotation.
#
# Usage: scripts/release.sh 0.7.0

set -euo pipefail

NEW="${1:?version required, e.g. 0.7.0}"
DATE=$(date +%Y-%m-%d)
TAG="v$NEW"
REPO_URL="https://gitlab.com/cynium/istota"

if ! git diff-index --quiet HEAD --; then
  echo "working tree dirty — commit or stash first" >&2
  exit 1
fi
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "tag $TAG already exists" >&2
  exit 1
fi
if ! grep -q '^## \[Unreleased\]$' CHANGELOG.md; then
  echo "no [Unreleased] section in CHANGELOG.md" >&2
  exit 1
fi

# Move [Unreleased] → [NEW] - DATE, add fresh empty [Unreleased] above,
# update link references at bottom.
python3 - "$NEW" "$DATE" "$REPO_URL" <<'PY'
import re, sys, pathlib
new, date, url = sys.argv[1:]
path = pathlib.Path("CHANGELOG.md")
text = path.read_text()
text = text.replace(
    "## [Unreleased]",
    f"## [Unreleased]\n\n## [{new}] - {date}",
    1,
)
old_unrel = re.search(r"^\[Unreleased\]:.*$", text, re.M).group(0)
new_unrel = f"[Unreleased]: {url}/-/compare/v{new}...main"
new_link = f"[{new}]: {url}/-/releases/v{new}"
text = text.replace(old_unrel, f"{new_unrel}\n{new_link}")
path.write_text(text)
PY

# Bump pyproject.toml version
sed -i.bak -E "s/^version = \".*\"/version = \"$NEW\"/" pyproject.toml
rm pyproject.toml.bak

# Extract just-published version's section as the tag body
NOTES=$(awk -v v="$NEW" '
  $0 ~ "^## \\[" v "\\]" {capture=1; next}
  capture && /^## \[/ {exit}
  capture {print}
' CHANGELOG.md)

if [ -z "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ]; then
  echo "extracted release notes for $TAG are empty" >&2
  exit 1
fi

git add CHANGELOG.md pyproject.toml
git commit -m "Bump version to $NEW"
git tag -a "$TAG" -m "Release $TAG" -m "$NOTES"
git push --follow-tags

echo
echo "Released $TAG to origin."
echo "GitHub Release will be created when the mirror picks up the tag."
