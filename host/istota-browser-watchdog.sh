#!/usr/bin/env bash
# The browser container's watchdog, on the VM (ISSUE-143, ISSUE-384).
#
# Ported from the Ansible role's istota-browser-watchdog.sh.j2 to run against
# the compose stack. Compose's healthcheck marks the container unhealthy, but
# `restart: unless-stopped` acts only on process exit, so something outside the
# container has to act on the verdict:
#
#   - a debounced restart, only after DEBOUNCE consecutive unhealthy reads;
#   - crash-loop protection: past CRASHLOOP_MAX restarts in CRASHLOOP_WINDOW
#     seconds it stops touching the container and alerts once;
#   - an alert on every restart, louder on a crash loop;
#   - one scheduled restart a day, after DAILY_RESTART_HOUR, which also clears
#     the crash-loop state.
#
# The healthcheck probes the liveness endpoint, so unhealthy means wedged, not
# busy. Of the faults its deep tier sees, an API process that can no longer
# drive a healthy Chrome is the one only a container restart clears.
#
# Run every minute by istota-browser-watchdog.timer. Plain file, not a
# template: settings come from /srv/istota/host.env through the unit.
set -euo pipefail

CONTAINER="${ISTOTA_BROWSER_CONTAINER:-istota-browser}"
STATE_DIR="${ISTOTA_BROWSER_WATCHDOG_STATE_DIR:-/var/lib/istota/browser-watchdog}"
LOG_FILE="${ISTOTA_BROWSER_WATCHDOG_LOG:-/var/log/istota/browser-health.log}"
DEBOUNCE="${ISTOTA_BROWSER_WATCHDOG_DEBOUNCE:-2}"
CRASHLOOP_MAX="${ISTOTA_BROWSER_WATCHDOG_CRASHLOOP_MAX:-5}"
CRASHLOOP_WINDOW="${ISTOTA_BROWSER_WATCHDOG_CRASHLOOP_WINDOW:-1800}"
DAILY_RESTART_HOUR="${ISTOTA_BROWSER_DAILY_RESTART_HOUR:-5}"
NTFY_URL="${ISTOTA_BROWSER_WATCHDOG_NTFY_URL:-}"
ALERT_EMAIL="${ISTOTA_BROWSER_WATCHDOG_ALERT_EMAIL:-}"

UNHEALTHY_FILE="$STATE_DIR/consecutive_unhealthy"
RESTARTS_FILE="$STATE_DIR/restart_timestamps"
TRIPPED_FILE="$STATE_DIR/crashloop_tripped"
DAILY_FILE="$STATE_DIR/last_daily_restart"

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

log() { echo "$(date -Is) $*" >> "$LOG_FILE"; }

alert() {
    # $1 = ntfy priority (default|high|urgent); rest = message
    local prio="$1"; shift
    local msg="$*"
    log "ALERT[$prio] $msg"
    if [ -n "$NTFY_URL" ]; then
        curl -fsS -H "Title: istota browser watchdog" \
            -H "Priority: $prio" -d "$msg" "$NTFY_URL" >/dev/null 2>&1 \
            || log "ntfy alert delivery failed"
    fi
    if [ -n "$ALERT_EMAIL" ] && command -v mail >/dev/null 2>&1; then
        echo "$msg" | mail -s "istota browser watchdog" "$ALERT_EMAIL" 2>/dev/null || true
    fi
}

restart_container() {
    log "Restarting $CONTAINER"
    # `--profile browser` because the service sits behind that profile, and a
    # profile service is invisible to a compose command that does not name it.
    # Through istota-stack, which knows the stack's compose files and settings.
    local stack="${ISTOTA_STACK_BIN:-/usr/local/sbin/istota-stack}"
    "$stack" compose --profile browser restart browser >> "$LOG_FILE" 2>&1 \
        || "$stack" compose --profile browser up -d browser >> "$LOG_FILE" 2>&1 \
        || log "restart command failed"
}

# Append the current restart, prune entries older than the window, echo the
# remaining count.
record_restart_and_count() {
    local now cutoff kept count
    now=$(date +%s)
    echo "$now" >> "$RESTARTS_FILE"
    cutoff=$(( now - CRASHLOOP_WINDOW ))
    kept=$(awk -v c="$cutoff" '$1 >= c' "$RESTARTS_FILE" 2>/dev/null || true)
    printf '%s\n' "$kept" | sed '/^$/d' > "$RESTARTS_FILE"
    # awk rather than `grep -c`, which exits 1 on no match.
    count=$(awk 'END {print NR}' "$RESTARTS_FILE" 2>/dev/null || echo 0)
    echo "$count"
}

daily_restart_due() {
    local today hour
    today=$(date +%F)
    hour=$(date +%-H)
    [ "$hour" -ge "$DAILY_RESTART_HOUR" ] && [ "$(cat "$DAILY_FILE" 2>/dev/null || true)" != "$today" ]
}

cmd_daily_restart() {
    log "Scheduled daily restart"
    restart_container
    date +%F > "$DAILY_FILE"
    rm -f "$TRIPPED_FILE" "$UNHEALTHY_FILE" "$RESTARTS_FILE"
}

cmd_check() {
    if daily_restart_due; then
        cmd_daily_restart
        return 0
    fi

    if [ -f "$TRIPPED_FILE" ]; then
        log "crash-loop tripped; skipping auto-restart (clear $TRIPPED_FILE to resume)"
        return 0
    fi

    local status
    status=$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")

    if [ "$status" = "healthy" ]; then
        : > "$UNHEALTHY_FILE"
        return 0
    fi
    if [ "$status" = "starting" ]; then
        return 0
    fi

    # unhealthy, missing, or "<no value>" for a container with no healthcheck.
    local n
    n=$(cat "$UNHEALTHY_FILE" 2>/dev/null || echo 0); n=$((n + 1))
    echo "$n" > "$UNHEALTHY_FILE"
    log "status=$status consecutive_unhealthy=$n/$DEBOUNCE"
    if [ "$n" -lt "$DEBOUNCE" ]; then
        return 0
    fi
    : > "$UNHEALTHY_FILE"

    local restarts
    restarts=$(record_restart_and_count)
    if [ "$restarts" -gt "$CRASHLOOP_MAX" ]; then
        touch "$TRIPPED_FILE"
        alert urgent "$CONTAINER crash-looping: $restarts restarts in ${CRASHLOOP_WINDOW}s. Auto-restart DISABLED pending investigation. Clear $TRIPPED_FILE to resume."
        return 0
    fi

    alert high "$CONTAINER unhealthy (status=$status) — restarting (restart #$restarts in last ${CRASHLOOP_WINDOW}s)."
    restart_container
}

case "${1:-check}" in
    check) cmd_check ;;
    daily-restart) cmd_daily_restart ;;
    *) echo "usage: $0 {check|daily-restart}" >&2; exit 2 ;;
esac
