#!/bin/bash
set -e

# MacBook Pro 14" scaled resolution (1440x900 is the default "looks like" setting)
SCREEN_WIDTH=${SCREEN_WIDTH:-1440}
SCREEN_HEIGHT=${SCREEN_HEIGHT:-900}
CERT_DIR="/data/browser-profile/ssl"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-/data/browser-profile}"

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

# Clean up stale Xvfb lock files from previous container runs
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start Xvfb (virtual display) — runs as root, X11 accepts all connections (-ac)
Xvfb :99 -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x24 -ac &
export DISPLAY=:99

# Wait for Xvfb to be ready (verify display is accepting connections)
echo "Waiting for Xvfb..."
for i in $(seq 1 30); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "Xvfb ready"
        break
    fi
    sleep 0.5
done
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "ERROR: Xvfb failed to start after 15 seconds"
    exit 1
fi

# Start x11vnc
VNC_ARGS="-display :99 -forever -shared -rfbport 5900"
if [ -n "$VNC_PASSWORD" ]; then
    VNC_ARGS="$VNC_ARGS -passwd $VNC_PASSWORD"
fi
x11vnc $VNC_ARGS &

# Start noVNC websocket proxy (serves web UI on port 6080, with TLS)
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 \
    --cert "$CERT_DIR/combined.pem" &

# Clean up stale Chromium profile locks from previous container runs
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonCookie" "$PROFILE_DIR/SingletonSocket"


# Start the Flask API as the non-root browser user
# This allows Chrome to use its native sandbox (Chrome refuses to sandbox as root)
exec su -s /bin/bash browser -c "DISPLAY=:99 LANG=$LANG TZ=$TZ BROWSER_PROFILE_DIR=$PROFILE_DIR python /app/browse_api.py"
