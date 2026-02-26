FROM python:3.12-slim-bookworm

# Set system locale so Chrome and Intl APIs use en_US natively
# (avoids needing CDP Emulation.setLocaleOverride which only affects main page)
ENV LANG=en_US.UTF-8
ENV TZ=America/New_York

# Install system deps: VNC, noVNC, Google Chrome, and utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    x11vnc \
    novnc \
    xvfb \
    x11-utils \
    xdotool \
    wget \
    curl \
    openssl \
    gnupg \
    locales \
    && sed -i '/en_US.UTF-8/s/^# //' /etc/locale.gen && locale-gen \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user so Chrome doesn't show --no-sandbox infobar
RUN groupadd -r browser && useradd -r -g browser -G audio,video -d /home/browser -m browser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Patchright system deps (shared libs Chrome needs)
RUN patchright install-deps chromium

# Patch Patchright source to remove "playwright" from identifiers.
# These patches are still useful when Patchright connects via CDP.
RUN find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i 's/injected-playwright-init-script-/x-init-/g' {} + && \
    find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i "s/__playwright_utility_world__/__chrome_utility_world__/g" {} +

# Copy stealth extension source files
COPY stealth-extension/manifest.json stealth-extension/stealth.js \
     stealth-extension/autoconsent-prefix.js stealth-extension/autoconsent-suffix.js \
     /app/stealth-extension/

# Download autoconsent bundle and build the extension's autoconsent.js
RUN curl -fsSL -o /tmp/autoconsent-bundle.js \
    "https://cdn.jsdelivr.net/npm/@duckduckgo/autoconsent@14.28.0/dist/autoconsent.playwright.js" && \
    cat /app/stealth-extension/autoconsent-prefix.js \
        /tmp/autoconsent-bundle.js \
        /app/stealth-extension/autoconsent-suffix.js \
        > /app/stealth-extension/autoconsent.js && \
    rm /tmp/autoconsent-bundle.js \
       /app/stealth-extension/autoconsent-prefix.js \
       /app/stealth-extension/autoconsent-suffix.js

COPY browse_api.py chrome.py browsing.py xdotool.py ./
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Set up directories the browser user needs write access to
RUN mkdir -p /data/browser-profile /tmp/.X11-unix && \
    chown -R browser:browser /data/browser-profile /app

# API port and noVNC port
EXPOSE 9223 6080

# Restart container if browser API or Chrome is unresponsive
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:9223/health | python -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('browser_connected',False) else 1)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
