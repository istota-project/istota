#!/bin/bash
# Setup script for istota

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Setting up istota..."

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "Error: uv not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Create virtual environment and install dependencies
echo "Installing dependencies..."
uv sync

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
