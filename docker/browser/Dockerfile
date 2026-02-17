FROM python:3.12-slim-bookworm

# Set system locale so Chrome and Intl APIs use en_US natively
# (avoids needing CDP Emulation.setLocaleOverride which only affects main page)
ENV LANG=en_US.UTF-8

# Install system deps: VNC, noVNC, Google Chrome, and utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    x11vnc \
    novnc \
    xvfb \
    x11-utils \
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

# Create non-root user so Chrome can use its native sandbox
# (Chrome refuses to sandbox as root: crbug.com/638180, shows yellow infobar warning)
RUN groupadd -r browser && useradd -r -g browser -G audio,video -d /home/browser -m browser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Patchright system deps (shared libs Chrome needs)
RUN patchright install-deps chromium

# Patch Patchright's init script tag to remove "playwright" from DOM elements.
# Patchright injects <script class="injected-playwright-init-script-{hex}"> elements
# that bot detectors can find via querySelectorAll('script[class*="playwright"]').
RUN find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i 's/injected-playwright-init-script-/x-init-/g' {} + && \
    find /usr/local/lib/python3.12/site-packages/patchright -name 'crPage.js' \
    -exec sed -i "s/__playwright_utility_world__/__chrome_utility_world__/g" {} +

# Download autoconsent bundle for cookie consent handling
RUN mkdir -p /app/vendor && \
    curl -fsSL -o /app/vendor/autoconsent.playwright.js \
    "https://cdn.jsdelivr.net/npm/@duckduckgo/autoconsent@14.28.0/dist/autoconsent.playwright.js"

COPY browse_api.py .
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
