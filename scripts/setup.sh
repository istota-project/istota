#!/bin/bash
# Setup script for istota

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Setting up istota..."

# Configure git hooks (pre-commit secret + private-data scan)
echo "Configuring git hooks..."
git config core.hooksPath .githooks
echo "  Git hooks configured"

# Presence is not enough to report the gate as armed: `gitleaks git` arrived in
# 8.19 and Debian 13 ships 8.16, so an apt-installed binary is present, looks
# fine here, and fails at the first commit. Probe the subcommand the hook
# actually calls.
if ! command -v gitleaks &> /dev/null; then
    gitleaks_problem="not found"
elif ! gitleaks git --help &> /dev/null; then
    gitleaks_problem="too old (no \`git\` subcommand — that arrived in 8.19)"
else
    gitleaks_problem=""
fi

if [ -n "$gitleaks_problem" ]; then
    echo "  WARNING: gitleaks $gitleaks_problem"
    echo "  Half the pre-commit gate is inactive: the private-data scan matches"
    echo "  patterns somebody wrote down, and gitleaks is what catches a"
    echo "  credential by shape and entropy. Nothing else covers that."
    echo "  Install: brew install gitleaks (macOS), or the release tarball from"
    echo "  https://github.com/gitleaks/gitleaks/releases (Linux)"
fi

if [ ! -f ".private-data-local" ]; then
    echo "  No .private-data-local yet — copy .private-data-local.example and add"
    echo "  your own names, hostnames and account numbers (the file is gitignored)"
fi

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "Error: uv not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Create virtual environment and install dependencies.
#
# Not a bare `uv sync`: that installs the base dependencies only, and the suite
# needs eight of the optional groups (click from money, fastapi from location
# and web, and so on). A bare sync leaves several hundred ModuleNotFoundError
# collection errors, which is a big enough number to read as a broken checkout
# rather than as a missing package.
#
# `test` is `all` minus the two heavy ML extras — memory-search (torch,
# sentence-transformers) and whisper (faster-whisper, av, onnxruntime). The
# suite runs clean without them, at 291 MB against 1.1 GB; the one test that
# needs them carries the `ml` marker and is deselected by default. Add
# --all-extras if you want that test, or the real libraries to hand-test with.
# See docs/development/testing.md.
#
# No --group flag: uv installs the default groups, so `dev` — pytest, its
# plugins, jinja2, psutil and ruff — arrives with this. That is why ruff belongs
# in the group rather than in an extra: `ruff check` is a documented
# verification step and until ISSUE-301 this command installed no linter at all.
echo "Installing dependencies..."
uv sync --extra test

# Create data directory
mkdir -p data

# Initialize database
echo "Initializing database..."
uv run python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'src')
from istota.db import init_db
init_db(Path('data/tasks.db'))
print('Database initialized at data/tasks.db')
"

# Create config from example if it doesn't exist
if [ ! -f "config/config.toml" ]; then
    cp config/config.example.toml config/config.toml
    echo "Created config/config.toml from example - please edit with your settings"
fi

# Create temp directory
mkdir -p /tmp/istota

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit config/config.toml with your Nextcloud credentials"
echo "2. Configure rclone: rclone config (create remote named 'nextcloud')"
echo "3. Add user resources: uv run istota resource add -u <user> -t calendar -p <calendar-path>"
echo "4. Test locally: uv run istota task 'What time is it?' -u testuser -x"
echo ""
echo "To run the webhook server:"
echo "  uv run istota-scheduler"
echo ""
echo "To run the scheduler (add to cron):"
echo "  uv run istota-scheduler"
