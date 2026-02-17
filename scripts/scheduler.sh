#!/bin/bash
# Cron wrapper for istota scheduler
# Add to crontab: * * * * * /path/to/istota/scripts/scheduler.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOCKFILE="/tmp/istota-scheduler.lock"
LOGFILE="/tmp/istota-scheduler.log"

# Prevent concurrent runs
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "$(date): Scheduler already running, exiting" >> "$LOGFILE"
    exit 0
fi

cd "$PROJECT_DIR"

# Run scheduler
uv run istota-scheduler --config config/config.toml 2>&1 | while read line; do
    echo "$(date): $line" >> "$LOGFILE"
done
