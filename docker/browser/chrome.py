"""Chrome process and CDP connection management.

Chrome is launched directly via subprocess with --remote-debugging-port.
Patchright connects lazily via connect_over_cdp for content extraction.
Navigation itself is driven by xdotool keystrokes into the omnibox rather than
by CDP (see xdotool.navigate), which is what keeps a debugger out of the
navigation path; the connection is not torn down around it.
"""

import logging
import os
import signal
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

# How many times Chrome has been launched in this process, counting the first.
#
# It exists because a session in browse_api is a *tab index*, not a page object,
# and a relaunch resets Chrome to a single about:blank tab while leaving the
# session table untouched. Every request carrying a pre-kill session id then
# resolved to a tab that no longer exists and returned "Tab not found" for the
# rest of the 600s TTL -- a recovery that worked, reported to the client as a
# fault, for ten minutes after the fact.
#
# A counter rather than a callback into browse_api, for two reasons. It keeps
# the dependency pointing one way (browse_api imports chrome, never the
# reverse), and it means the invalidation happens on the Flask thread, when the
# session is next looked up, rather than from the watchdog thread that did the
# relaunch. Mutating the session table from that thread would be a smaller
# version of the mistake ISSUE-382 is about.
#
# Written under _chrome_lock, which every launch already holds. Read without it:
# an int assignment is atomic under the GIL and a reader comparing against a
# value one generation stale errs towards discarding a live session, which costs
# a new tab and nothing else.
_launch_generation = 0

# When the browse watchdog last had to kill and relaunch Chrome.
#
# One recovery is the watchdog working: a page wedged, it was killed, the next
# request succeeded. A *run* of them is a container that cannot stay up, and
# before ISSUE-394 nothing anywhere could tell the two apart -- the deep probe's
# CDP arm only observes connect_cdp(), and the successful connect after each
# relaunch zeroes its counter, so a wedge every 90s read as healthy forever and
# only a human noticed.
#
# The list is capped rather than pruned by age here: pruning needs the window,
# the window is the probe's policy and lives in browse_api, and this module
# records without drawing conclusions -- the same split as _cdp_health above.
WEDGE_HISTORY_MAX = 64
_wedge_recoveries = []
_wedge_lock = threading.Lock()

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

# The CDP heartbeat: what the last CDP-touching call did, and when (ISSUE-384).
#
# ISSUE-382's fix made a poisoned Patchright binding a contained error instead of
# a silent one. It did not make it *visible*: the container's deep liveness probe
# asks whether the Chrome process is alive and whether Chrome's DevTools endpoint
# answers, and through the whole of that outage both were true. What was dead was
# this process's own ability to drive the browser, and nothing measured it. So the
# one fault that needs a restart was the one nothing detected, and the outage
# ended at the unrelated 05:00 proactive restart rather than by detection.
#
# The probe cannot ask by trying: calling Patchright from the liveness thread is
# the exact mistake ISSUE-382 is about. So the request thread publishes what
# happened instead and the probe reads it. Every write here is a plain
# assignment under a leaf lock -- no Patchright, no I/O, nothing that can block --
# which is what makes the record readable from any thread when connect_cdp() is
# not.
#
# This module records; it draws no conclusion. The threshold and the staleness
# window are the probe's policy and live in browse_api, so a change to how
# aggressively the container restarts itself does not touch the connection code.
#
# Only a genuine CDP success clears the failure count. Notably a Chrome restart
# does not: recover_wedged_chrome() kills and relaunches the browser, which
# repairs a wedged Chrome and does nothing at all for a poisoned asyncio loop --
# that state survives for the life of the *process*. Treating a relaunch as
# recovery would hide the one fault this record exists to expose.
#
# Two rules decide what is even eligible to be counted, and both exist because a
# count is a container restart. Getting either wrong costs a killed browsing
# session for a container that was working.
#
# 1. Only a call the caller cares about. connect_cdp() is reached from teardown
#    and from diagnostics as well as from a request, and those callers wrap it in
#    `except Exception: pass` -- a failure there is not evidence that anything a
#    client asked for went wrong, and those paths can never produce a
#    compensating success either. `record=False` is how they say so. Three
#    /interact calls carrying stale session ids used to reach the threshold on
#    their own, via _cleanup_expired() at the top of every endpoint, and return
#    404 to the client with nothing having actually failed.
#
# 2. Only a failure Chrome does not explain. This is the signature ISSUE-384
#    actually names: a run of CDP failures *with Chrome alive*. If Chrome is
#    gone, restarting, or not answering on DevTools, a failed connect says
#    nothing about this process's binding -- and the probe's first two arms
#    already cover exactly those three states, so counting them here would only
#    add a stale count that outlives the condition. The measured case is a
#    legitimate recovery: recover_wedged_chrome() kills Chrome, the unwinding
#    request calls _close_session() -> get_context() during the relaunch, and
#    that used to record a failure for a recovery that worked.
#
# What survives both rules is: a request-path CDP call that failed while Chrome
# was up and answering. That is the ISSUE-382 shape and very little else.
_cdp_health = {
    "last_success": 0.0,
    "last_failure": 0.0,
    "consecutive_failures": 0,
    "last_error": "",
}
# The two timestamps are time.monotonic(), not wall clock. Nothing renders them
# -- /health reports the count and the error text, and the probe turns
# last_failure into an age -- so they are pure in-process durations, and a
# monotonic clock means an NTP step cannot re-arm a verdict that had aged out or
# clear a live one early. A backwards step of more than the staleness window did
# exactly the former.
#
# A leaf lock: acquired only around the assignments below and the copy in
# cdp_health(). cleanup() reaches it while holding _chrome_lock, so the ordering
# is always _chrome_lock -> _cdp_health_lock and never the reverse; the liveness
# thread takes this one alone. Nothing that can block is called while it is held
# -- _chrome_explains_failure() runs before it is taken, because it makes an HTTP
# call.
_cdp_health_lock = threading.Lock()


def _record_cdp_success():
    """Note that a CDP call worked. Safe from any thread."""
    with _cdp_health_lock:
        _cdp_health["last_success"] = time.monotonic()
        _cdp_health["consecutive_failures"] = 0
        _cdp_health["last_error"] = ""


def _chrome_explains_failure():
    """Whether Chrome's own state accounts for a CDP call having failed.

    Down, mid-relaunch, or not answering on DevTools. Each is a condition the
    liveness probe's first two arms report on their own, and none of them says
    anything about whether this process's Patchright binding still works.

    Called before the health lock is taken: devtools_responding() is an HTTP
    call, and holding a lock the liveness thread wants across it is the shape
    this module exists to avoid.
    """
    if not is_chrome_running() or _launching:
        return True
    return not devtools_responding(timeout=2)


def _record_cdp_failure(error):
    """Note that a CDP call could not be made. Safe from any thread.

    One call is one failure. connect_cdp() retries internally and this is
    recorded only once it has given up, so the count is a run of failed
    *requests* rather than a multiple of the retry setting.

    A failure Chrome explains is recorded but not counted: the error text and
    the timestamp are useful for diagnosis either way, and the count is what
    drives a restart. Returns whether it counted, for the caller's logging.
    """
    explained = _chrome_explains_failure()
    with _cdp_health_lock:
        _cdp_health["last_failure"] = time.monotonic()
        _cdp_health["last_error"] = str(error)[:500]
        if not explained:
            _cdp_health["consecutive_failures"] += 1
    return not explained


def cdp_health():
    """Snapshot of the CDP heartbeat. Safe from any thread.

    A copy, so a caller cannot mutate the record it is reporting on, and so the
    fields a verdict is computed from are read as one consistent set.
    """
    with _cdp_health_lock:
        return dict(_cdp_health)


def launch_generation():
    """How many times Chrome has been launched in this process. Any thread."""
    return _launch_generation


def record_wedge_recovery():
    """Note that the browse watchdog had to kill and relaunch Chrome.

    Safe from any thread: one append under a leaf lock, nothing that can block.
    """
    with _wedge_lock:
        _wedge_recoveries.append(time.monotonic())
        if len(_wedge_recoveries) > WEDGE_HISTORY_MAX:
            del _wedge_recoveries[:-WEDGE_HISTORY_MAX]


def wedge_recovery_history():
    """Copy of the recovery timestamps (time.monotonic). Safe from any thread."""
    with _wedge_lock:
        return list(_wedge_recoveries)


def _signal_group(proc, sig):
    """Signal the whole process group Chrome leads. Returns whether it did.

    Chrome's renderers, GPU process, zygotes and utility processes are children
    of the *browser* process, not of this one, so signalling the handle alone
    orphans every one of them -- they reparent to PID 1, which in this container
    is ``su`` and reaps nothing. ``start_new_session=True`` at launch puts the
    whole tree in a group of its own so one signal reaches all of it.

    The leadership check is a safety interlock, not a formality. If the process
    somehow shares this one's group -- ``start_new_session`` unsupported, a
    handle that came from somewhere else -- then killpg on that group would
    signal the API process itself. Returning False sends the caller to the
    single-process fallback instead.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return False
    if pgid != proc.pid:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except Exception:
        return False


def _kill_chrome_proc(proc, timeout=5):
    """Stop Chrome and reap it. Never raises.

    The reap is the part that was missing. The old shape was ``terminate()``,
    ``wait(timeout=5)``, and on the exception ``kill()`` with nothing after it --
    so the one case that reached ``kill()`` was exactly the case that left a
    zombie: a Chrome whose UI thread is blocked ignores SIGTERM, times out the
    wait, takes SIGKILL, and is never collected. This process is not PID 1 and
    reaps nothing implicitly, so that child stayed defunct for the life of the
    process. 43 of them accumulated in 40 minutes of production.

    Never raises because one caller is the browse watchdog's thread, where an
    exception is reported as a heal that did not happen.
    """
    if proc is None:
        return
    try:
        if not _signal_group(proc, signal.SIGTERM):
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except Exception:
        pass
    try:
        if not _signal_group(proc, signal.SIGKILL):
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        log.warning("Chrome pid %s did not reap after SIGKILL", getattr(proc, "pid", "?"))


def _dismiss_dialog(dialog):
    """Answer a JavaScript dialog so it cannot hold the renderer main thread.

    An alert, confirm, prompt or beforeunload blocks the renderer's main thread
    until something answers it, and in this container nothing can: Xvfb runs
    with no window manager, and xdotool targets the largest window on the
    display -- the browser, never a modal child of it. The compositor keeps
    animating and DevTools keeps answering, so the container reads healthy
    throughout. Dismiss rather than accept: a beforeunload is the common case
    and dismissing it is the one that lets navigation proceed.

    Never raises. It runs on Patchright's event dispatcher, where an exception
    is not attributable to any request and can take the connection with it.
    """
    try:
        log.info(
            "Dismissing %s dialog: %s",
            getattr(dialog, "type", "?"), (getattr(dialog, "message", "") or "")[:200],
        )
    except Exception:
        pass
    try:
        dialog.dismiss()
    except Exception:
        pass


def _install_dialog_guards(context):
    """Register the dialog handler on a context and every page it opens.

    Patchright dismisses dialogs by default only on pages it is attached to and
    only silently, which is why this is explicit: a popup opened by the page
    under test was covered by nothing, and a dismissal nobody logged was
    invisible when diagnosing a wedge.

    Never raises. A page can close between being listed and being registered,
    and a connect must not fail because of it.
    """
    def _guard(page):
        try:
            page.on("dialog", _dismiss_dialog)
        except Exception:
            pass

    try:
        context.on("page", _guard)
    except Exception:
        pass
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    for page in pages:
        _guard(page)


def launch_chrome():
    """Launch Chrome directly with debugging port and stealth extension."""
    global _chrome_proc, _launching, _launch_generation

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
        # Modal browser UI nothing in this container can dismiss. Xvfb runs with
        # no window manager, xdotool addresses the largest window on the display
        # rather than a modal child of it, and each of these blocks Chrome's UI
        # or renderer main thread while the compositor keeps animating and
        # DevTools keeps answering -- so the container reads healthy for as long
        # as it lasts. --disable-hang-monitor is the load-bearing one: without it
        # a slow renderer earns a "Page unresponsive" dialog, which is a wedge
        # produced by the recovery UI for a wedge. The crash bubble matters here
        # specifically because the browse watchdog hard-kills Chrome against a
        # persistent --user-data-dir, so every relaunch is a crash-restore boot.
        "--noerrdialogs",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-session-crashed-bubble",
        "--disable-print-preview",
        f"--disable-extensions-except={EXTENSION_DIR}",
        f"--load-extension={EXTENSION_DIR}",
        "about:blank",
    ]

    # Chrome's log goes nowhere by default, and that is the fix rather than an
    # oversight (ISSUE-394). It used to be `--enable-logging=stderr` into
    # `stderr=subprocess.PIPE`, with no reader anywhere in this package: the
    # wrapper script at /usr/bin/google-chrome-stable routes Chrome's stderr
    # through a `cat` into that pipe, the 64 KiB buffer filled after two or three
    # page renders -- measured on the production container at 15900, then 46445,
    # then 63199 of 65536 bytes across three renders, with the `cat` blocked in
    # pipe_write -- and the next log write on Chrome's browser UI thread blocked
    # forever. CDP hung, xdotool clicks did nothing, CSS animations kept running
    # on the compositor thread of another process, and /json/version kept
    # answering on the browser IO thread, so all three arms of /live?deep=1
    # stayed green while the container was unusable.
    #
    # CHROME_LOG_STDERR is the way back to the log, and it *inherits* rather than
    # pipes: the container's own stderr is drained by the Docker log collector,
    # so it cannot fill. subprocess.PIPE must never appear here again -- it is
    # the production wedge, and behind a flag it would read like a debugging
    # convenience.
    log_stderr = os.environ.get("CHROME_LOG_STDERR", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if log_stderr:
        args[-1:-1] = ["--enable-logging=stderr", "--v=0"]

    env = {**os.environ, "DISPLAY": ":99"}
    with _chrome_lock:
        _launching = True
        try:
            _chrome_proc = subprocess.Popen(
                args, env=env,
                stdout=subprocess.DEVNULL,
                # None means inherit the container's stderr; never PIPE.
                stderr=None if log_stderr else subprocess.DEVNULL,
                # A session of its own, so _signal_group can reach the renderers,
                # GPU process and zygotes in one signal instead of orphaning them.
                start_new_session=True,
            )
            _launch_generation += 1
            _wait_for_chrome_ready()
            log.info(
                "Chrome launched (pid=%d, debug_port=%d, generation=%d)",
                _chrome_proc.pid, CHROME_PORT, _launch_generation,
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
        _kill_chrome_proc(_chrome_proc)
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
        _kill_chrome_proc(proc)
        launch_chrome()
    # Recorded after the relaunch, and outside the lock, so the record describes
    # a recovery that actually happened. The verdict drawn from it lives in
    # browse_api with the rest of the liveness policy.
    record_wedge_recovery()


def is_chrome_running():
    """Check if Chrome process is alive."""
    return _chrome_proc is not None and _chrome_proc.poll() is None


def _assert_pw_thread(op, record=True):
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
    err = RuntimeError(
        f"chrome.{op}() called from thread {threading.current_thread().name!r}, "
        f"which does not own the CDP connection",
    )
    # A refusal is the other way this class of fault presents, so it feeds the
    # same heartbeat (ISSUE-384). The reasoning is that a refusal means *this
    # call could not drive the browser*, which is the question the probe asks --
    # and when the refused caller is the request thread, it is the permanent
    # lockout ISSUE-382's own review found (a foreign thread winning the first
    # connect leaves the Flask thread refused for the life of the process).
    # Nothing at this raise site can tell that apart from a stray background
    # caller, and the two need the same answer anyway: only a genuine CDP
    # success clears the count, so a container still serving requests resets it
    # continuously and a stray refusal never accumulates on its own.
    if record:
        _record_cdp_failure(err)
    raise err


def connect_cdp(retries=3, record=True):
    """Connect Patchright to Chrome via CDP (lazy, idempotent).

    Retries on failure because Patchright's driver can crash when
    connecting to pages with complex/navigating frame trees.

    ``record=False`` keeps the outcome out of the CDP heartbeat. Pass it from
    any caller that treats a failure as nothing -- teardown, eviction,
    diagnostics -- since the heartbeat drives a container restart and those
    callers swallow the exception and never produce a compensating success. See
    the _cdp_health comment.
    """
    global _pw, _pw_browser, _pw_context, _pw_thread_id
    _assert_pw_thread("connect_cdp", record=record)
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
            # A completed round-trip over the socket is as much evidence that
            # this process can drive the browser as a fresh connect is. Recorded
            # even under record=False: a success is never a reason to restart,
            # so there is no false positive to protect against, and a teardown
            # that reached the browser is real evidence the binding works.
            _record_cdp_success()
            return
        except Exception:
            disconnect_cdp()
    last_error = None
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
            _install_dialog_guards(_pw_context)
            log.debug("CDP connected")
            _record_cdp_success()
            return
        except Exception as e:
            last_error = e
            log.warning(
                "CDP connect attempt %d/%d failed: %s",
                attempt + 1, retries, e,
            )
            disconnect_cdp()
            if attempt < retries - 1:
                time.sleep(1)
    # Carry the last attempt's cause into the message. Without it the only thing
    # a caller ever saw was "Failed to connect CDP after retries", which named
    # neither the poisoned-loop message nor anything else, and the eight hours of
    # ISSUE-382 needed a container log read to tell apart from a dead Chrome.
    err = RuntimeError(f"Failed to connect CDP after {retries} retries: {last_error}")
    if record and _record_cdp_failure(err):
        log.error(
            "CDP failed with Chrome up and answering -- counted against the "
            "liveness heartbeat (ISSUE-384): %s", err,
        )
    raise err


def disconnect_cdp():
    """Disconnect Patchright from Chrome (Chrome keeps running).

    Guarded like the rest: _pw.stop() drives the same thread-bound machinery as
    any other call, so tearing the connection down from a foreign thread wedges
    the loop exactly as using it would. A watchdog that needs Chrome gone
    without touching Patchright calls recover_wedged_chrome() instead.
    """
    global _pw, _pw_browser, _pw_context, _pw_thread_id
    # record=False: this is teardown. It is reached from connect_cdp()'s own
    # failure paths, from _close_session_unlocked() and from cleanup(), and in
    # each the caller either records the real outcome itself or does not care.
    _assert_pw_thread("disconnect_cdp", record=False)
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


def get_context(record=True):
    """Get the Patchright browser context (connects if needed).

    ``record`` passes straight through to connect_cdp(); see the note there.
    """
    connect_cdp(record=record)
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
            _kill_chrome_proc(_chrome_proc)
            _chrome_proc = None
