#!/usr/bin/env bash
# Offline deployment window. Arguments: namespace, service user, istota binary,
# config path, lock wait seconds, optional --lock-held (the update cron only).
set -euo pipefail
namespace=$1
service_user=$2
istota_bin=$3
config_path=$4
lock_wait=$5
if [ "${6:-}" != "--lock-held" ]; then
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

result=0
# Let systemd parse its own EnvironmentFile; it is not a shell script. The
# trusted CLI needs the same Nextcloud credential as the stopped services.
output=$(systemd-run --quiet --wait --pipe --collect --uid="$service_user" \
    --property="EnvironmentFile=-/etc/${namespace}/secrets.env" \
    "$istota_bin" -c "$config_path" init --relocate-rooms 2>&1) || result=$?
printf '%s\n' "$output"
if [ "$result" -ne 0 ]; then
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
