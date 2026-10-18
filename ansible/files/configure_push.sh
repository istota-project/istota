#!/bin/bash
set -euo pipefail
# Use the installed stack's lock, container privilege drop and restart path.
# shellcheck disable=SC1090
source "$1"
inputs="$2"
take_stack_lock istota_push
rc=0
ISTOTA_CONFIG_READ_ONLY=false oneshot -v "${inputs}:/push-input:ro" \
    "$(main_service)" python /push-input/configure_push.py /push-input || rc=$?
case "$rc" in
    0|2)
        if [ "$rc" -eq 2 ]; then touch "$RESTART_OWED"; fi
        if [ -f "$RESTART_OWED" ]; then
            restart_for_config || exit 1
            rc=2
        fi
        exit "$rc" ;;
    3)
        touch "$RESTART_OWED"
        echo 'Push configuration partially changed; correct the failure and rerun istota_push.' >&2
        exit 1 ;;
    *) exit 1 ;;
esac
