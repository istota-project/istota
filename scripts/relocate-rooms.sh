#!/usr/bin/env bash
# Offline deployment window. Arguments: namespace, service user, istota binary,
# config path, lock wait seconds, database path, optional --lock-held (cron only).
set -euo pipefail
namespace=$1
service_user=$2
istota_bin=$3
config_path=$4
lock_wait=$5
db_path=$6
if [ "${7:-}" != "--lock-held" ]; then
    exec 200>"/tmp/${namespace}-update.lock"
    flock -w "$lock_wait" 200
fi

running=()
restore_writers() {
    local result=$?
    trap - EXIT
    for unit in ${running[@]+"${running[@]}"}; do
        systemctl start "$unit" || result=1
    done
    exit "$result"
}
trap restore_writers EXIT
for suffix in scheduler web webhooks; do
    unit="${namespace}-${suffix}"
    if systemctl is-active --quiet "$unit"; then
        running+=("$unit")
    fi
    load_state=$(systemctl show "$unit" --property=LoadState --value)
    case "$load_state" in
        loaded) systemctl stop "$unit" ;;
        not-found) ;;
        *) echo "Cannot stop $unit: LoadState=$load_state" >&2; exit 1 ;;
    esac
done

# Root provisioning may leave SQLite sidecars owned by root. Repair ownership
# only after every writer has stopped, so sidecars cannot vanish during chown.
# An absent main database or a failed ownership change must stop migration.
chown "$service_user:" "$db_path"
for sidecar in "$db_path-wal" "$db_path-shm"; do
    if [ -e "$sidecar" ]; then
        chown "$service_user:" "$sidecar"
    fi
done

result=0
# Let systemd parse its own EnvironmentFile; it is not a shell script. The
# trusted CLI needs the same Nextcloud credential as the stopped services.
output=$(systemd-run --quiet --wait --pipe --collect --uid="$service_user" \
    --property="EnvironmentFile=-/etc/${namespace}/secrets.env" \
    "$istota_bin" -c "$config_path" init --relocate-rooms 2>&1) || result=$?
printf '%s\n' "$output"
# Exit 2 is a partial: what could move moved, the rest is re-listed by every
# later sweep, and the CLI has raised an admin alert naming it. Failing here
# made the cron redeploy every tick, stopping and restarting every unit each
# time (ISSUE-588), so it counts as deployed and the next deploy retries.
if [ "$result" -eq 2 ]; then
    echo "WARNING: room migration left work outstanding; the next deployment retries it"
elif [ "$result" -ne 0 ]; then
    if [ "$result" -ne 1 ] || ! grep -qx 'refusal: live_tasks' <<< "$output"; then
        exit "$result"
    fi
    echo "Room migration deferred: live tasks; retry on the next deployment"
fi
# Only previously active services resume. Restart gives each one the new code.
for unit in ${running[@]+"${running[@]}"}; do
    systemctl restart "$unit"
done
trap - EXIT
