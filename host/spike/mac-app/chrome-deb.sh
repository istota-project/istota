#!/bin/bash
# mac-desktop-app Stage 1 (e): does Chrome's .deb install with no further
# download on top of an image that already has what it depends on? Run as root
# in a VM with Docker and the network on (the base needs it; the Chrome layer
# is built with --network=none).
#
# The base is the browser image's apt step without Chrome, plus whatever apt
# says Chrome's .deb would pull in, recommends included (the browser Dockerfile
# installs the .deb without --no-install-recommends).
set -euo pipefail
WORK=/var/tmp/spike-chrome
rm -rf "$WORK"; mkdir -p "$WORK/base" "$WORK/chrome"
t() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(t)" 'BEGIN { printf "%.1f", b - a }'; }
arch="$(dpkg --print-architecture)"
deb="google-chrome-stable_current_${arch}.deb"
curl -fsSL -o "$WORK/chrome/$deb" "https://dl.google.com/linux/direct/${deb}"
cp "$WORK/chrome/$deb" "$WORK/base/$deb"
echo "chrome .deb: $(stat -c %s "$WORK/chrome/$deb") bytes, $(dpkg-deb -f "$WORK/chrome/$deb" Version)"

cat > "$WORK/base/Dockerfile" <<'EOF'
FROM python:3.12-slim-trixie
ARG DEB
COPY ${DEB} /tmp/chrome.deb
# The browser Dockerfile's packages, Chrome left out; then what apt would add
# for Chrome, read from a dry run against the .deb and installed by name.
RUN set -eux; apt-get update; \
    apt-get install -y --no-install-recommends tini util-linux x11vnc novnc python3-websockify \
        xvfb x11-utils xdotool wget curl openssl gnupg locales fonts-noto-color-emoji \
        fonts-unifont fonts-liberation fonts-liberation-sans-narrow fonts-dejavu-core \
        fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf \
        xfonts-scalable libfontconfig1 libfreetype6; \
    deps="$(apt-get install -s /tmp/chrome.deb | awk '/^Inst / && $2 != "google-chrome-stable" { print $2 }')"; \
    echo "chrome pulls in: $(echo $deps | wc -w) packages"; \
    apt-get install -y $deps; \
    rm -f /tmp/chrome.deb
EOF
t0="$(t)"
docker build -q -t spike-browser-base --build-arg DEB="$deb" "$WORK/base" >/dev/null
echo "base build (network on): $(since "$t0") s, $(docker image inspect --format '{{.Size}}' spike-browser-base) bytes"

cat > "$WORK/chrome/Dockerfile" <<'EOF'
FROM spike-browser-base
ARG DEB
COPY ${DEB} /tmp/chrome.deb
# `apt-get install --no-download ./x.deb` fails on apt's own "Pathname to
# install is not absolute" even when nothing is missing; the plan is printed
# for the record and dpkg installs, apt-get check proving nothing is unmet.
RUN set -eux; apt-get install -s /tmp/chrome.deb | grep -E '^(Inst|[0-9]+ upgraded)'; \
    dpkg -i /tmp/chrome.deb; apt-get check; rm -f /tmp/chrome.deb; \
    google-chrome --version
EOF
t0="$(t)"
rc=0
docker build --network=none --progress=plain -t spike-browser-chrome --build-arg DEB="$deb" "$WORK/chrome" \
    > "$WORK/chrome-build.log" 2>&1 || rc=$?
echo "chrome layer (--network=none): exit ${rc}, $(since "$t0") s"
grep -E "Google Chrome|^#[0-9]+ [0-9.]+ Inst|newly installed|E: |Err:|Need to get|dpkg: " "$WORK/chrome-build.log" | sed -n 1,10p
[ "$rc" -ne 0 ] || echo "image: $(docker image inspect --format '{{.Size}}' spike-browser-chrome) bytes"
