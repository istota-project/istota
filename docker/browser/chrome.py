"""Chrome process and CDP connection management.

Chrome is launched directly via subprocess with --remote-debugging-port.
Patchright connects lazily via connect_over_cdp for content extraction,
and disconnects before navigation so Cloudflare cannot detect a debugger.
"""

import logging
import os
import subprocess
import time
import urllib.request

from patchright.sync_api import sync_playwright

log = logging.getLogger(__name__)

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/browser-profile")
EXTENSION_DIR = "/app/stealth-extension"
CHROME_PORT = 9222

# Chrome process
_chrome_proc = None

# Patchright CDP connection (lazy)
_pw = None
_pw_browser = None
_pw_context = None


def launch_chrome():
    """Launch Chrome directly with debugging port and stealth extension."""
    global _chrome_proc

    chrome_path = os.environ.get(
        "CHROME_EXECUTABLE", "/usr/bin/google-chrome-stable",
    )
    screen_w = int(os.environ.get("SCREEN_WIDTH", "1440"))
    screen_h = int(os.environ.get("SCREEN_HEIGHT", "900"))

    args = [
        chrome_path,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={CHROME_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US,en",
        f"--window-size={screen_w},{screen_h}",
        "--window-position=0,0",
        "--enable-unsafe-swiftshader",
        "--use-gl=swiftshader",
        "--enable-webgl",
        "--enable-features=SharedArrayBuffer",
        "--disable-features=DnsOverHttps",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--enable-logging=stderr",
        "--v=0",
        f"--disable-extensions-except={EXTENSION_DIR}",
        f"--load-extension={EXTENSION_DIR}",
        "about:blank",
    ]

    env = {**os.environ, "DISPLAY": ":99"}
    _chrome_proc = subprocess.Popen(
        args, env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    _wait_for_chrome_ready()
    log.info(
        "Chrome launched (pid=%d, debug_port=%d)",
        _chrome_proc.pid, CHROME_PORT,
    )


def _wait_for_chrome_ready(timeout=15):
    """Wait for Chrome's debugging port to accept connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(
                f"http://localhost:{CHROME_PORT}/json/version", timeout=2,
            )
            resp.close()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(
        f"Chrome not ready on port {CHROME_PORT} after {timeout}s",
    )


def ensure_chrome():
    """Ensure Chrome process is running, relaunch if dead."""
    global _chrome_proc
    if _chrome_proc is not None and _chrome_proc.poll() is None:
        return
    log.warning("Chrome not running -- launching")
    disconnect_cdp()
    launch_chrome()


def restart_chrome():
    """Kill and restart Chrome."""
    global _chrome_proc
    disconnect_cdp()
    if _chrome_proc:
        try:
            _chrome_proc.terminate()
            _chrome_proc.wait(timeout=5)
        except Exception:
            try:
                _chrome_proc.kill()
            except Exception:
                pass
    _chrome_proc = None
    launch_chrome()


def is_chrome_running():
    """Check if Chrome process is alive."""
    return _chrome_proc is not None and _chrome_proc.poll() is None


def connect_cdp(retries=3):
    """Connect Patchright to Chrome via CDP (lazy, idempotent).

    Retries on failure because Patchright's driver can crash when
    connecting to pages with complex/navigating frame trees.
    """
    global _pw, _pw_browser, _pw_context
    if _pw_browser is not None:
        try:
            _ = _pw_browser.contexts
            return
        except Exception:
            disconnect_cdp()
    for attempt in range(retries):
        try:
            _pw = sync_playwright().start()
            _pw_browser = _pw.chromium.connect_over_cdp(
                f"http://localhost:{CHROME_PORT}",
            )
            contexts = _pw_browser.contexts
            _pw_context = contexts[0] if contexts else _pw_browser.new_context()
            log.debug("CDP connected")
            return
        except Exception as e:
            log.warning(
                "CDP connect attempt %d/%d failed: %s",
                attempt + 1, retries, e,
            )
            disconnect_cdp()
            if attempt < retries - 1:
                time.sleep(1)
    raise RuntimeError("Failed to connect CDP after retries")


def disconnect_cdp():
    """Disconnect Patchright from Chrome (Chrome keeps running)."""
    global _pw, _pw_browser, _pw_context
    try:
        if _pw_browser:
            _pw_browser.close()
    except Exception:
        pass
    try:
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = None
    _pw_browser = None
    _pw_context = None
    log.debug("CDP disconnected")


def is_cdp_connected():
    """Check if Patchright is currently connected to Chrome."""
    return _pw_browser is not None


def get_context():
    """Get the Patchright browser context (connects if needed)."""
    connect_cdp()
    return _pw_context


def get_page_by_index(tab_index):
    """Get a page by tab index from the connected context."""
    if not _pw_context:
        return None
    pages = _pw_context.pages
    if tab_index < len(pages):
        return pages[tab_index]
    return None


def cleanup():
    """Clean up CDP connection and Chrome process."""
    global _chrome_proc
    disconnect_cdp()
    if _chrome_proc:
        try:
            _chrome_proc.terminate()
            _chrome_proc.wait(timeout=5)
        except Exception:
            try:
                _chrome_proc.kill()
            except Exception:
                pass
        _chrome_proc = None
