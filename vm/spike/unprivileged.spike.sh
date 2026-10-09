#!/bin/bash
# Stage 1 spike: the unprivileged phase. Renders a config with the current
# render-config.sh (no Nextcloud reachable; only the daemon's existence and
# privileges are measured here), runs `istota init`, then execs the daemon
# from the venv directly rather than through `uv run`.
set -euo pipefail

log() { echo "[spike-unpriv] $*"; }
export HOME=/data/home
CONFIG=/data/config/config.toml

log "uid=$(id -u) gid=$(id -g) groups=$(id -G) caps: $(grep -E '^Cap(Prm|Eff|Bnd)' /proc/self/status | tr '\n' ' ')"

if [ ! -f "$CONFIG" ]; then
    CONFIG_FILE="$CONFIG" USER_NAME=spike NC_URL=http://nextcloud.invalid \
        APP_PASSWORD=spike-app-password BOT_USER=istota /render-config.sh >/dev/null
    log "rendered $CONFIG"
fi

if [ ! -f /data/.secret_key ]; then
    ( umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > /data/.secret_key )
fi
ISTOTA_SECRET_KEY="$(cat /data/.secret_key)"
export ISTOTA_SECRET_KEY

/app/.venv/bin/istota -c "$CONFIG" init >/dev/null
log "istota init: ok"

if [ "${SPIKE_DAEMON:-1}" = "1" ]; then
    log "exec istota-scheduler --daemon"
    exec /app/.venv/bin/istota-scheduler --daemon -c "$CONFIG"
fi
log "SPIKE_DAEMON=0: holding without a daemon"
exec sleep infinity
