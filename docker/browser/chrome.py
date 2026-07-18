"""Chrome process and CDP connection management.

Chrome is launched directly via subprocess with --remote-debugging-port.
Patchright connects lazily via connect_over_cdp for content extraction,
and disconnects before navigation so Cloudflare cannot detect a debugger.
"""

import logging
import os
import subprocess
import threading
import time
import urllib.request

from patchright.sync_api import sync_playwright

log = logging.getLogger(__name__)

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/browser-profile")
EXTENSION_DIR = "/app/stealth-extension"
CHROME_PORT = 9222

# Serializes the Chrome OS-process lifecycle (launch/ensure/restart/recover/
# cleanup) across the threads that touch it: the Flask request thread and the
# browse-watchdog thread. Without it, the watchdog's recover_wedged_chrome()
# nulls _chrome_proc and relaunches while a freshly-unblocked Flask request runs
# ensure_chrome() concurrently -- two Popen()s race for the same --user-data-dir
# and --remote-debugging-port, orphaning one Chrome (ISSUE-173 follow-up).
# Reentrant because ensure_chrome/restart_chrome/recover_wedged_chrome call
# launch_chrome() while already holding it. Deliberately does NOT guard the
# CDP/Patchright helpers (connect_cdp/get_context/...): those run on the Flask
# thread *during* a browse, so locking them would let a wedged browse hold this
# lock and deadlock the very watchdog meant to kill it. The wedge always sits in
# a CDP call, which holds no lock here, so the watchdog can always acquire it.
_chrome_lock = threading.RLock()

# Chrome process
_chrome_proc = None

# True while launch_chrome() is bringing Chrome up: the process exists but its
# DevTools endpoint isn't serving yet, so a deep liveness probe must not read
# that window as a wedge (ISSUE-149).
_launching = False

# Patchright CDP connection (lazy)
_pw = None
_pw_browser = None
_pw_context = None


def launch_chrome():
    """Launch Chrome directly with debugging port and stealth extension."""
    global _chrome_proc, _launching

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
        "--renderer-process-limit=4",
        "--js-flags=--max-old-space-size=256",
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
    with _chrome_lock:
        _launching = True
        try:
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
        finally:
            _launching = False


def devtools_responding(timeout=2):
    """Whether Chrome's DevTools HTTP endpoint answers within ``timeout`` seconds.

    The endpoint is served by the Chrome browser process itself, independent of
    the single-threaded Flask app and of page-level browse work: a long browse
    holds the Flask thread, not Chrome's DevTools server, so this keeps answering
    fast while a browse is in flight but stops answering when the browser process
    is genuinely wedged. That is the discriminator a process-only ``poll()`` can't
    see (ISSUE-149) — a wedged-but-alive Chrome accepts the TCP connection but
    never sends an HTTP response, so the short timeout is what catches it.
    """
    try:
        resp = urllib.request.urlopen(
            f"http://localhost:{CHROME_PORT}/json/version", timeout=timeout,
        )
        resp.close()
        return True
    except Exception:
        return False


def is_launching():
    """Whether a launch is in progress (DevTools not yet expected to answer)."""
    return _launching


def _wait_for_chrome_ready(timeout=15):
    """Wait for Chrome's debugging port to accept connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if devtools_responding(timeout=2):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome not ready on port {CHROME_PORT} after {timeout}s",
    )


def ensure_chrome():
    """Ensure Chrome process is running, relaunch if dead."""
    global _chrome_proc
    with _chrome_lock:
        if _chrome_proc is not None and _chrome_proc.poll() is None:
            return
        log.warning("Chrome not running -- launching")
        disconnect_cdp()
        launch_chrome()


def restart_chrome():
    """Kill and restart Chrome."""
    global _chrome_proc
    with _chrome_lock:
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


def recover_wedged_chrome():
    """Kill and relaunch Chrome from a watchdog thread (Playwright-free).

    Like restart_chrome(), but it does NOT call disconnect_cdp(): Patchright's
    sync objects are bound to the Flask thread that created them and must never
    be touched from another thread. Killing the Chrome OS process is enough to
    unblock a wedged in-flight CDP call on the Flask thread — that call raises
    when the process dies, the request unwinds, and the stale Patchright
    connection is rebuilt lazily by the next connect_cdp() (which already
    re-probes and disconnects a dead browser). Only touches the subprocess
    handle, the urllib readiness probe, and _chrome_lock -- never Patchright --
    so this is the variant the browse watchdog calls while the Flask thread is
    blocked (ISSUE-149 renderer/session wedge; ISSUE-173).

    Takes _chrome_lock so it can't race a concurrent launch on the Flask thread
    (the double-Popen orphan). This cannot deadlock against the wedged request:
    the wedge sits in a CDP call, which holds no lock here; the lock is only ever
    held briefly (a bounded launch/kill), never across the CDP call being killed.
    """
    global _chrome_proc
    with _chrome_lock:
        proc, _chrome_proc = _chrome_proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
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
        # Verify the existing CDP connection is genuinely live before reusing it.
        # Neither `.contexts` nor is_connected() is reliable here: both read
        # cached local state, so a websocket killed from the watchdog thread
        # (recover_wedged_chrome) isn't noticed until Patchright's sync dispatcher
        # happens to pump the disconnect event -- until then a dead connection
        # reports live and hands back a page bound to a closed socket (ISSUE-173
        # follow-up). new_browser_cdp_session() forces a real round-trip over the
        # socket and raises at once if it's dead, regardless of dispatcher timing.
        try:
            session = _pw_browser.new_browser_cdp_session()
            try:
                session.detach()
            except Exception:
                pass
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
    with _chrome_lock:
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
