#!/bin/bash
set -e

# MacBook Pro 14" scaled resolution (1440x900 is the default "looks like" setting)
SCREEN_WIDTH=${SCREEN_WIDTH:-1440}
SCREEN_HEIGHT=${SCREEN_HEIGHT:-900}
CERT_DIR="/data/browser-profile/ssl"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-/data/browser-profile}"
BROWSER_RUNTIME_DIR="/run/istota-browser"

# A token passed in the environment (no compose file) stays out of the children below.
INHERITED_TOKEN="${ISTOTA_BROWSER_API_TOKEN:-}"
unset ISTOTA_BROWSER_API_TOKEN

as_browser() {
    setpriv --reuid=browser --regid=browser --init-groups "$@"
}

# Ensure browser user owns the profile directory (volume may be owned by root)
chown -R browser:browser "$PROFILE_DIR"

# Chrome refuses to start without its sandbox, which needs this. The API stays up
# so /health can answer; the failed launch is what a caller sees.
if ! as_browser unshare --user --pid --net --fork true 2>/dev/null; then
    echo "istota-browser: cannot create a user namespace, so Chrome will not start." \
        "The service needs docker/istota/seccomp-istota.json (compose sets it) and" \
        "user namespaces on the host." >&2
fi

# Generate self-signed cert on first run (persisted in volume). As the browser
# user, since Chrome's uid can write this directory and root opens no file in it.
if [ ! -f "$CERT_DIR/cert.pem" ]; then
    as_browser sh -c '
        set -e
        mkdir -p "$1"
        openssl req -x509 -newkey rsa:2048 -keyout "$1/key.pem" \
            -out "$1/cert.pem" -days 3650 -nodes \
            -subj "/CN=stealth-browser"
        cat "$1/key.pem" "$1/cert.pem" > "$1/combined.pem"
    ' sh "$CERT_DIR"
fi

# The non-root pool starts one X server per instance. Prepare its socket dir.
install -d -m 1777 /tmp/.X11-unix
# Slots start at 100; these are stale files from a previous container process.
rm -f /tmp/.X1*-lock /tmp/.X11-unix/X1*

# Token and index files are transient. The browser user publishes both; the
# packaged noVNC tree is root-owned, so serve a writable runtime copy.
rm -rf "$BROWSER_RUNTIME_DIR"
install -d "$BROWSER_RUNTIME_DIR/web" "$BROWSER_RUNTIME_DIR/vnc-tokens"
cp -a /usr/share/novnc/. "$BROWSER_RUNTIME_DIR/web/"
printf '[]\n' > "$BROWSER_RUNTIME_DIR/web/instances.json"
chown -R browser:browser "$BROWSER_RUNTIME_DIR"

# The web container's console relay reaches each x11vnc through a socket in
# this shared volume: browser writes, group 10001 (web's gid) may only traverse.
# No volume, no directory: the pool then starts x11vnc without the socket.
CONSOLE_DIR="/run/istota-browser-console"
if [ -d "$CONSOLE_DIR" ]; then
    install -d -o browser -g 10001 -m 2710 "$CONSOLE_DIR"
    find "$CONSOLE_DIR" -mindepth 1 -maxdepth 1 -name '*.sock' -delete
fi

# One TLS listener routes operator viewers to the requested live instance. As the
# browser user: it serves files from a tree that user writes, and as root it
# followed a link there to the token's secret file.
as_browser websockify --web "$BROWSER_RUNTIME_DIR/web" --cert "$CERT_DIR/combined.pem" \
    --token-plugin TokenFile --token-source "$BROWSER_RUNTIME_DIR/vnc-tokens" 6080 &

# Chrome's only way out (egress_proxy.py). Restarted, not waited for: while it is
# down Chrome reaches nothing.
(
    while true; do
        as_browser python -s /app/egress_proxy.py || true
        sleep 1
    done
) &

# The API's service token (#884). Read here because the file is uid 10001's and
# the browser user is not. Exported after the children above started, and never
# passed as an argument below, where tini's argv would show it to the whole
# container.
TOKEN_FILE=/run/secrets/istota_browser_api_token
if [ -s "$TOKEN_FILE" ]; then
    ISTOTA_BROWSER_API_TOKEN="$(cat "$TOKEN_FILE")"
fi
export ISTOTA_BROWSER_API_TOKEN="${ISTOTA_BROWSER_API_TOKEN:-$INHERITED_TOKEN}"

# Forward Docker stop to the API only, then reap orphaned children. su kills
# its shell after two seconds, before the API can flush every Chrome profile.
# setpriv execs directly and preserves the configured environment without a
# second shell parsing paths or values. Keep the browser account's HOME too, and
# -s, because a user site under it is Chrome's uid's to write and would run here
# with the token at the next start.
exec tini -- setpriv --reuid=browser --regid=browser --init-groups \
    env HOME=/home/browser USER=browser LOGNAME=browser SHELL=/bin/bash \
    BROWSER_PROFILE_DIR="$PROFILE_DIR" python -s /app/browse_api.py
