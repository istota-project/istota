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
#
# The lock is not the only rule about threads here, and the two are easy to
# confuse. This one is about *serialization*; _assert_pw_thread below is about
# *thread identity*, and it acquires nothing -- so guarding the CDP helpers with
# it is compatible with the paragraph above rather than an exception to it.
# Together they split the lifecycle functions this comment lists into two halves,
# and only one of them is safe from the watchdog thread:
#
#   cross-thread: launch_chrome, recover_wedged_chrome (touch the OS process only)
#   Flask-thread-only: ensure_chrome, restart_chrome, cleanup (all reach
#       disconnect_cdp, which is Patchright and therefore thread-bound)
#
# Calling one of the second group from the watchdog raises rather than wedging
# the process. That is the intended outcome, but it means "the lock makes this
# safe from any thread" is no longer true of all five.
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

# The thread that opened the current CDP connection, and the only one allowed
# to touch it. Patchright's sync API drives a process-global asyncio loop
# through a per-thread greenlet: a call from a second thread abandons
# run_until_complete() without unwinding, so the loop keeps is_running() True
# and every later sync_playwright().start() raises "It looks like you are using
# Playwright Sync API inside the asyncio loop" -- for the life of the process,
# with Chrome still up and every health probe still green. disconnect_cdp()
# cannot repair it either, because _pw.stop() goes through the same broken
# machinery. That is ISSUE-382, and it cost eight hours of dead browsing.
#
# The invariant was documented for the browse watchdog (which is why
# recover_wedged_chrome() exists) but nothing enforced it, and the resource
# monitor broke it from its own thread. This makes it structural: a foreign
# caller gets a contained RuntimeError instead of poisoning the process.
# None means no connection is open, so any thread may claim the next one.
_pw_thread_id = None


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
    """Ensure Chrome process is running, relaunch if dead.

    Flask-thread-only: reaches disconnect_cdp(). A watchdog thread wanting Chrome
    back wants recover_wedged_chrome() instead. See the _chrome_lock comment.
    """
    global _chrome_proc
    with _chrome_lock:
        if _chrome_proc is not None and _chrome_proc.poll() is None:
            return
        log.warning("Chrome not running -- launching")
        disconnect_cdp()
        launch_chrome()


def restart_chrome():
    """Kill and restart Chrome.

    Flask-thread-only: reaches disconnect_cdp(). See the _chrome_lock comment.
    """
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
    global _chrome_proc, _pw_thread_id
    with _chrome_lock:
        # Release CDP ownership without touching Patchright. Killing Chrome
        # invalidates the connection anyway -- connect_cdp's round-trip probe
        # will find it dead and rebuild -- so the owning thread id is stale from
        # here on. Clearing it is what stops the guard becoming its own version
        # of ISSUE-382: if a non-Flask thread ever won the first connect_cdp(),
        # every Flask call would raise for the life of the process, with nothing
        # able to reset the field (disconnect_cdp is itself refused). This is the
        # recovery path, and it is safe here because a plain assignment is not a
        # Patchright call.
        _pw_thread_id = None
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


def _assert_pw_thread(op):
    """Refuse a Patchright call from a thread that doesn't own the connection.

    Inert when nothing is connected: the guard binds an existing connection to
    its opener, it does not reserve the module for one thread forever. Raises
    rather than returning a sentinel, because every caller here is on a request
    path where continuing without a browser is the wrong answer, and because a
    silent no-op is what let ISSUE-382 look healthy for eight hours.
    """
    if _pw_thread_id is None or threading.get_ident() == _pw_thread_id:
        return
    log.error(
        "chrome.%s() called from thread %r, but the CDP connection is owned by "
        "thread id %d -- refusing (ISSUE-382). Patchright's sync objects are "
        "bound to their opening thread; touching them from another one wedges "
        "the asyncio loop for the life of the process. Do the work on the Flask "
        "thread, or use recover_wedged_chrome() if you only need the OS process.",
        op, threading.current_thread().name, _pw_thread_id,
    )
    raise RuntimeError(
        f"chrome.{op}() called from thread {threading.current_thread().name!r}, "
        f"which does not own the CDP connection",
    )


def connect_cdp(retries=3):
    """Connect Patchright to Chrome via CDP (lazy, idempotent).

    Retries on failure because Patchright's driver can crash when
    connecting to pages with complex/navigating frame trees.
    """
    global _pw, _pw_browser, _pw_context, _pw_thread_id
    _assert_pw_thread("connect_cdp")
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
            # Claim ownership before creating anything, not after: assigning it
            # last leaves a window where _pw_context is live and the guard is
            # still inert, which is the one call the guard exists to refuse.
            # Every failure path below calls disconnect_cdp(), which clears all
            # four globals together, so an aborted attempt leaves nothing stale.
            _pw_thread_id = threading.get_ident()
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
    """Disconnect Patchright from Chrome (Chrome keeps running).

    Guarded like the rest: _pw.stop() drives the same thread-bound machinery as
    any other call, so tearing the connection down from a foreign thread wedges
    the loop exactly as using it would. A watchdog that needs Chrome gone
    without touching Patchright calls recover_wedged_chrome() instead.
    """
    global _pw, _pw_browser, _pw_context, _pw_thread_id
    _assert_pw_thread("disconnect_cdp")
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
    _pw_thread_id = None
    log.debug("CDP disconnected")


def is_cdp_connected():
    """Check if Patchright is currently connected to Chrome.

    Deliberately unguarded: a plain attribute read touches none of Patchright's
    thread-bound machinery, and the liveness probe and /health diagnostics ask
    this without owning the connection.
    """
    return _pw_browser is not None


def get_context():
    """Get the Patchright browser context (connects if needed)."""
    connect_cdp()
    return _pw_context


def get_page_by_index(tab_index):
    """Get a page by tab index from the connected context."""
    _assert_pw_thread("get_page_by_index")
    if not _pw_context:
        return None
    pages = _pw_context.pages
    if tab_index < len(pages):
        return pages[tab_index]
    return None


def cleanup():
    """Clean up CDP connection and Chrome process.

    Flask-thread-only: reaches disconnect_cdp(). Registered via atexit, which
    runs on the main thread -- the same thread Flask serves on, so it owns the
    connection. See the _chrome_lock comment.
    """
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
