#!/usr/bin/env bash
# Local end-to-end dev loop for the health module (and other in-tree work).
#
# What it does:
#   1. Creates .devstate/ (config, DB, temp, logs — gitignored).
#   2. Templates .devstate/config.toml with module_health enabled, sandbox /
#      skill-proxy / network off (darwin-friendly), Talk / email / Nextcloud
#      disabled. Existing .devstate/config.toml is preserved.
#   3. Initializes the DB and ensures a local "dev" user.
#   4. Runs the scheduler in the foreground with ISTOTA_CONFIG_PATH pointing at
#      the templated config.
#
# In another tab, drive it via the CLI:
#
#   export ISTOTA_CONFIG_PATH="$PWD/.devstate/config.toml"
#   uv run istota task "import bloodwork from /path/to/Bloodwork.csv" -u dev
#   uv run istota list -u dev
#   uv run istota show <task-id>
#
# Or run an end-to-end health-skill smoke without the scheduler:
#
#   HEALTH_DB_PATH=$PWD/.devstate/workspace/dev/health/data/health.db \
#   ISTOTA_EXPERIMENTAL_FEATURES=module_health \
#     uv run python -m istota.skills.health import-csv /path/to/Bloodwork.csv
#
# For web-UI work, the real /istota/web requires Nextcloud OAuth2 — use the
# mock-mode frontend instead for design iteration:
#
#   cd web && VITE_MOCK_API=1 npm run dev

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

DEV_DIR=".devstate"
CONFIG_PATH="$DEV_DIR/config.toml"
DB_PATH="$DEV_DIR/data/istota.db"
TEMP_DIR="$DEV_DIR/temp"
WORKSPACE_DIR="$DEV_DIR/workspace"
LOG_DIR="$DEV_DIR/logs"
DEV_USER="${ISTOTA_DEV_USER:-dev}"

mkdir -p "$DEV_DIR/data" "$TEMP_DIR" "$WORKSPACE_DIR" "$LOG_DIR"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "→ writing $CONFIG_PATH"
  cat > "$CONFIG_PATH" <<EOF
# Local dev config — module_health enabled, sandbox off.
# Regenerate by deleting this file and re-running scripts/dev-up.sh.

bot_name = "Istota-dev"
db_path = "$DB_PATH"
temp_dir = "$TEMP_DIR"
nextcloud_mount_path = "$PWD/$WORKSPACE_DIR"

[experimental]
features = ["module_health"]

[security]
sandbox_enabled = false
skill_proxy_enabled = false

[security.network]
enabled = false

[talk]
enabled = false

[email]
enabled = false

[nextcloud]
url = ""
username = ""
app_password = ""

[scheduler]
poll_interval = 2
task_timeout_minutes = 10

[logging]
level = "INFO"
output = "console"

[users.$DEV_USER]
display_name = "Dev"
email_addresses = []
timezone = "UTC"
# Opt out of every module except health so the scheduler doesn't auto-seed
# cron jobs (feeds/money) or hit a missing location.db.
disabled_modules = ["feeds", "money", "location"]
EOF
else
  echo "→ reusing existing $CONFIG_PATH"
fi

export ISTOTA_CONFIG_PATH="$PWD/$CONFIG_PATH"
# Synthesise a per-user workspace path the health loader will discover.
mkdir -p "$WORKSPACE_DIR/Users/$DEV_USER/Istota-dev"

echo "→ initializing DB at $DB_PATH"
uv run istota -c "$CONFIG_PATH" init

echo "→ ensuring user '$DEV_USER'"
uv run istota -c "$CONFIG_PATH" user ensure --name "$DEV_USER" --display-name Dev --tz UTC \
  --disabled-module feeds --disabled-module money --disabled-module location >/dev/null

echo
echo "Ready. Drive the loop from another tab:"
echo "  export ISTOTA_CONFIG_PATH=\"$PWD/$CONFIG_PATH\""
echo "  uv run istota task 'import bloodwork from /abs/path.csv' -u $DEV_USER"
echo "  uv run istota list -u $DEV_USER"
echo
echo "Scheduler logs → $LOG_DIR/scheduler.log (tee'd to stdout). Ctrl-C to stop."
echo

exec uv run istota-scheduler -d -v -c "$CONFIG_PATH" 2>&1 | tee "$LOG_DIR/scheduler.log"
