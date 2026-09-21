#!/bin/bash
set -e

# MacBook Pro 14" scaled resolution (1440x900 is the default "looks like" setting)
SCREEN_WIDTH=${SCREEN_WIDTH:-1440}
SCREEN_HEIGHT=${SCREEN_HEIGHT:-900}
CERT_DIR="/data/browser-profile/ssl"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-/data/browser-profile}"
BROWSER_RUNTIME_DIR="/run/istota-browser"

# Ensure browser user owns the profile directory (volume may be owned by root)
chown -R browser:browser "$PROFILE_DIR"

# Generate self-signed cert on first run (persisted in volume)
if [ ! -f "$CERT_DIR/cert.pem" ]; then
    mkdir -p "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 -keyout "$CERT_DIR/key.pem" \
        -out "$CERT_DIR/cert.pem" -days 3650 -nodes \
        -subj "/CN=stealth-browser"
    cat "$CERT_DIR/key.pem" "$CERT_DIR/cert.pem" > "$CERT_DIR/combined.pem"
    chown -R browser:browser "$CERT_DIR"
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

# One TLS listener routes operator viewers to the requested live instance.
websockify --web "$BROWSER_RUNTIME_DIR/web" --cert "$CERT_DIR/combined.pem" \
    --token-plugin TokenFile --token-source "$BROWSER_RUNTIME_DIR/vnc-tokens" 6080 &

# Forward Docker stop to the API only, then reap orphaned children. su kills
# its shell after two seconds, before the API can flush every Chrome profile.
# setpriv execs directly and preserves the configured environment without a
# second shell parsing paths or values. Keep the browser account's HOME too.
exec tini -- setpriv --reuid=browser --regid=browser --init-groups \
    env HOME=/home/browser USER=browser LOGNAME=browser SHELL=/bin/bash \
    BROWSER_PROFILE_DIR="$PROFILE_DIR" python /app/browse_api.py
