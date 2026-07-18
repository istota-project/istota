"""Browser automation API — Flask endpoints.

Chrome is launched directly (no Patchright ownership) with a stealth
extension for script injection. Patchright connects via CDP only for
content extraction, disconnecting before navigation so Cloudflare
cannot detect an attached debugger.
"""

import atexit
import logging
import os
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request

import chrome
import browsing
import xdotool

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Session management — sessions track Chrome tab indices
_sessions = {}  # id -> {tab_index, created_at}
_sessions_lock = threading.Lock()
SESSION_TTL = 600  # 10 minutes
MAX_SESSIONS = int(os.environ.get("MAX_BROWSER_SESSIONS", "2"))
MEMORY_REJECT_PCT = 85  # reject new sessions above this
MEMORY_EVICT_PCT = 80   # evict oldest idle session above this

# Browse watchdog — self-heals a renderer/session wedge the container health
# check is structurally blind to. Chrome's DevTools endpoint keeps answering
# during a per-page freeze, so /live?deep=1 stays green and the container
# watchdog never restarts (ISSUE-149's documented boundary, hit in prod as
# ISSUE-173). A wedged /browse then blocks the single Flask thread forever,
# burning the caller's whole timeout with no signal. This request-level
# watchdog kills+relaunches Chrome once any request outlives a hard deadline:
# the kill makes the wedged in-flight CDP call raise (fail fast) AND heals the
# browser for the next caller. Deadline must sit above the slowest legitimate
# browse (navigate + Cloudflare challenge + settle) to avoid killing a slow-
# but-live session; tune per deployment via the env var. 0 disables.
BROWSE_WATCHDOG_DEADLINE_S = int(os.environ.get("BROWSE_WATCHDOG_DEADLINE_S", "90"))
BROWSE_WATCHDOG_POLL_S = int(os.environ.get("BROWSE_WATCHDOG_POLL_S", "5"))
_inflight = None  # {"path", "url", "started"} for the one in-flight Flask request
_inflight_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_memory_pct():
    """Return container memory usage percentage, or 0 if unavailable."""
    try:
        with open("/sys/fs/cgroup/memory.current") as f:
            current = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max") as f:
            v = f.read().strip()
            limit = int(v) if v != "max" else None
        if limit:
            return round(current / limit * 100, 1)
    except Exception:
        pass
    return 0


def _create_session():
    """Create a new browser tab session.

    Raises RuntimeError if memory pressure is too high.
    """
    mem_pct = _get_memory_pct()
    if mem_pct > MEMORY_REJECT_PCT:
        raise RuntimeError(
            f"Memory pressure too high ({mem_pct}%), refusing new session"
        )

    chrome.connect_cdp()
    ctx = chrome.get_context()

    with _sessions_lock:
        _evict_expired()
        while len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda s: _sessions[s]["created_at"])
            _close_session_unlocked(oldest)

    ctx.new_page()
    tab_index = len(ctx.pages) - 1

    session_id = str(uuid.uuid4())[:8]
    with _sessions_lock:
        _sessions[session_id] = {
            "tab_index": tab_index,
            "created_at": time.time(),
        }
    return session_id, tab_index


def _get_session(session_id):
    """Get session info dict, or None if expired/missing."""
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session["created_at"] > SESSION_TTL:
            _sessions.pop(session_id, None)
            return None
        return session


def _close_session_unlocked(session_id):
    """Close a session and its tab (caller must hold lock).

    Closes the tab to free renderer processes, then adjusts tab indices
    for remaining sessions. If it's the last tab, navigates to about:blank
    instead (Chrome exits when all tabs close).
    """
    session = _sessions.pop(session_id, None)
    if not session:
        return
    closed_index = session["tab_index"]
    if chrome.is_cdp_connected():
        try:
            page = chrome.get_page_by_index(closed_index)
            if not page:
                return
            ctx = chrome.get_context()
            if len(ctx.pages) <= 1:
                # Last tab — navigate to blank instead of closing
                page.goto("about:blank", timeout=5000)
                return
            page.close()
        except Exception:
            pass
    # Shift down indices above the closed tab
    for s in _sessions.values():
        if s["tab_index"] > closed_index:
            s["tab_index"] -= 1


def _close_session(session_id):
    with _sessions_lock:
        _close_session_unlocked(session_id)


def _evict_expired():
    """Remove expired sessions and close their tabs. Caller must hold lock."""
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if now - s["created_at"] > SESSION_TTL
    ]
    for sid in expired:
        _close_session_unlocked(sid)


def _get_page(tab_index):
    """Get the Patchright page for a tab index (CDP must be connected)."""
    return chrome.get_page_by_index(tab_index)


# ---------------------------------------------------------------------------
# Navigation flow: disconnect CDP -> xdotool -> reconnect CDP
# ---------------------------------------------------------------------------

def _navigate_and_wait(tab_index, url, timeout_ms=30000):
    """Navigate via xdotool and wait for challenges.

    Chrome was launched with --remote-debugging-port (not --remote-debugging-pipe).
    The port mode doesn't signal an always-attached debugger to Chrome internals,
    unlike the pipe mode which Cloudflare detected. We keep CDP connected for
    simplicity and only use xdotool for navigation input.

    1. Focus the correct tab (if multiple tabs exist)
    2. Navigate via xdotool (pure X11 keyboard input)
    3. Wait for Cloudflare/security challenges to resolve
    4. Passive wait for page to settle
    """
    chrome.connect_cdp()

    # Focus the right tab if multiple exist
    ctx = chrome.get_context()
    if ctx and len(ctx.pages) > 1:
        page = chrome.get_page_by_index(tab_index)
        if page:
            page.bring_to_front()

    # Navigate via pure X11 input (not CDP Page.navigate)
    xdotool.navigate(url, timeout_s=timeout_ms // 1000)

    # Wait for Cloudflare/security challenges
    xdotool.wait_for_challenges(timeout_s=15)

    # Passive wait — let page JS and fingerprinting complete
    time.sleep(browsing.gauss_clamp(3.5, 1.0, 2.0, 5.0))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/browse", methods=["POST"])
def browse():
    """Navigate to URL and return page content."""
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url", "")
    session_id = data.get("session_id")
    timeout = data.get("timeout", 30) * 1000
    wait_for = data.get("wait_for")
    keep_session = data.get("keep_session", False)
    skip_behavior = data.get("skip_behavior", False)

    if not url:
        return jsonify({"error": "url is required"}), 400

    created_new = False
    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        tab_index = session["tab_index"]
    else:
        session_id, tab_index = _create_session()
        created_new = True

    try:
        _navigate_and_wait(tab_index, url, timeout_ms=timeout)

        page = _get_page(tab_index)
        if not page:
            raise RuntimeError("Tab not found after reconnection")

        browsing.wait_for_datadome(page)
        if not skip_behavior:
            browsing.simulate_human_behavior(page)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10000)
            except Exception:
                pass

        if browsing.detect_captcha(page):
            vnc_url = os.environ.get("BROWSER_VNC_URL", "")
            return jsonify({
                "status": "captcha",
                "session_id": session_id,
                "vnc_url": vnc_url,
                "message": "Captcha detected. Solve via VNC, then retry.",
            })

        content = browsing.extract_page_content(page)
        result = {"status": "ok", **content}

        if keep_session or not created_new:
            result["session_id"] = session_id
        else:
            _close_session(session_id)

        return jsonify(result)

    except Exception as e:
        if created_new and not keep_session:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/screenshot", methods=["POST"])
def screenshot():
    """Take a screenshot of the current page."""
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url")
    session_id = data.get("session_id")
    full_page = data.get("full_page", False)
    timeout = data.get("timeout", 30) * 1000

    created_new = False
    tab_index = None

    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        tab_index = session["tab_index"]
    elif url:
        session_id, tab_index = _create_session()
        created_new = True
        try:
            _navigate_and_wait(tab_index, url, timeout_ms=timeout)
            page = _get_page(tab_index)
            if page:
                browsing.wait_for_datadome(page)
                browsing.simulate_human_behavior(page)
        except Exception as e:
            _close_session(session_id)
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        chrome.connect_cdp()
        page = _get_page(tab_index)
        if not page:
            raise RuntimeError("Tab not found")
        img_bytes = page.screenshot(full_page=full_page)
        if created_new:
            _close_session(session_id)
        return Response(img_bytes, mimetype="image/png")
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/extract", methods=["POST"])
def extract():
    """Extract content by CSS selector."""
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url")
    session_id = data.get("session_id")
    selector = data.get("selector", "body")
    timeout = data.get("timeout", 30) * 1000

    created_new = False
    tab_index = None

    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        tab_index = session["tab_index"]
    elif url:
        session_id, tab_index = _create_session()
        created_new = True
        try:
            _navigate_and_wait(tab_index, url, timeout_ms=timeout)
            page = _get_page(tab_index)
            if page:
                browsing.wait_for_datadome(page)
                browsing.simulate_human_behavior(page)
        except Exception as e:
            _close_session(session_id)
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        chrome.connect_cdp()
        page = _get_page(tab_index)
        if not page:
            raise RuntimeError("Tab not found")

        elements = page.query_selector_all(selector)
        results = []
        for el in elements[:20]:
            text = el.inner_text().strip()
            html = el.inner_html()
            if text:
                entry = {"text": text[:10000], "html": html[:10000]}
                for attr in ("href", "src", "data-link-name", "id", "class"):
                    val = el.get_attribute(attr)
                    if val:
                        entry[attr] = val[:500]
                results.append(entry)

        if created_new:
            _close_session(session_id)

        return jsonify({
            "status": "ok",
            "url": page.url,
            "selector": selector,
            "count": len(results),
            "elements": results,
        })
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/interact", methods=["POST"])
def interact():
    """Interact with an existing session (click, fill, scroll)."""
    _cleanup_expired()
    data = request.get_json()
    session_id = data.get("session_id")
    actions = data.get("actions", [])

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    session = _get_session(session_id)
    if not session:
        return jsonify({
            "error": f"session {session_id} not found or expired",
        }), 404

    chrome.connect_cdp()
    page = _get_page(session["tab_index"])
    if not page:
        return jsonify({"error": "tab not found"}), 500

    results = []
    try:
        for action in actions:
            action_type = action.get("type")
            selector = action.get("selector", "")

            if action_type == "click":
                page.click(selector, timeout=10000)
                page.wait_for_timeout(1000)
                results.append({
                    "action": "click", "selector": selector, "ok": True,
                })
            elif action_type == "fill":
                value = action.get("value", "")
                page.fill(selector, value, timeout=10000)
                results.append({
                    "action": "fill", "selector": selector, "ok": True,
                })
            elif action_type == "scroll":
                direction = action.get("direction", "down")
                amount = action.get("amount", 500)
                if direction == "down":
                    page.evaluate(f"window.scrollBy(0, {amount})")
                elif direction == "up":
                    page.evaluate(f"window.scrollBy(0, -{amount})")
                results.append({
                    "action": "scroll", "direction": direction, "ok": True,
                })
            elif action_type == "wait":
                timeout_ms = action.get("timeout", 2000)
                page.wait_for_timeout(min(timeout_ms, 30000))
                results.append({"action": "wait", "ok": True})
            elif action_type == "select":
                value = action.get("value", "")
                page.select_option(selector, value, timeout=10000)
                results.append({
                    "action": "select", "selector": selector, "ok": True,
                })
            else:
                results.append({
                    "action": action_type, "ok": False, "error": "unknown",
                })

        if browsing.detect_captcha(page):
            vnc_url = os.environ.get("BROWSER_VNC_URL", "")
            return jsonify({
                "status": "captcha",
                "session_id": session_id,
                "vnc_url": vnc_url,
                "actions": results,
            })

        content = browsing.extract_page_content(page)
        return jsonify({
            "status": "ok",
            "session_id": session_id,
            "actions": results,
            **content,
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "session_id": session_id,
            "actions": results,
            "error": str(e),
        }), 500


@app.route("/evaluate", methods=["POST"])
def evaluate():
    """Evaluate JavaScript in an existing session."""
    _cleanup_expired()
    data = request.get_json()
    session_id = data.get("session_id")
    expression = data.get("expression", "")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    if not expression:
        return jsonify({"error": "expression is required"}), 400

    session = _get_session(session_id)
    if not session:
        return jsonify({
            "error": f"session {session_id} not found or expired",
        }), 404

    chrome.connect_cdp()
    page = _get_page(session["tab_index"])
    if not page:
        return jsonify({"error": "tab not found"}), 500

    try:
        result = page.evaluate(expression)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/sessions/<session_id>", methods=["GET"])
def get_session_info(session_id):
    """Check session status."""
    session = _get_session(session_id)
    if not session:
        return jsonify({"status": "not_found"}), 404
    age = time.time() - session["created_at"]
    ttl = max(0, SESSION_TTL - age)
    # Try to get URL if CDP is connected
    url = ""
    if chrome.is_cdp_connected():
        page = chrome.get_page_by_index(session["tab_index"])
        if page:
            try:
                url = page.url
            except Exception:
                pass
    return jsonify({
        "status": "active",
        "session_id": session_id,
        "age_seconds": int(age),
        "ttl_seconds": int(ttl),
        "url": url,
    })


@app.route("/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Close a session."""
    _close_session(session_id)
    return jsonify({"status": "closed", "session_id": session_id})


# ---------------------------------------------------------------------------
# Health and monitoring
# ---------------------------------------------------------------------------

def _get_chrome_diagnostics():
    """Collect Chrome process and memory diagnostics."""
    diag = {}
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5,
        )
        chrome_procs = []
        total_rss_kb = 0
        for line in result.stdout.splitlines():
            if "chrome" in line.lower() and "--type=" in line:
                parts = line.split()
                rss_kb = int(parts[5])
                proc_type = "unknown"
                for arg in line.split():
                    if arg.startswith("--type="):
                        proc_type = arg.split("=", 1)[1]
                        break
                chrome_procs.append({
                    "type": proc_type, "rss_mb": rss_kb // 1024,
                })
                total_rss_kb += rss_kb
        diag["chrome_processes"] = len(chrome_procs)
        diag["chrome_rss_mb"] = total_rss_kb // 1024
        diag["process_detail"] = chrome_procs
    except Exception as e:
        diag["chrome_process_error"] = str(e)

    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            current_bytes = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            max_val = f.read().strip()
        max_bytes = int(max_val) if max_val != "max" else None
        diag["container_memory_mb"] = current_bytes // (1024 * 1024)
        if max_bytes:
            diag["container_memory_limit_mb"] = max_bytes // (1024 * 1024)
            diag["container_memory_pct"] = round(
                current_bytes / max_bytes * 100, 1,
            )
    except Exception:
        pass

    try:
        diag["chrome_running"] = chrome.is_chrome_running()
        diag["cdp_connected"] = chrome.is_cdp_connected()
        if chrome.is_cdp_connected():
            ctx = chrome.get_context()
            diag["browser_pages"] = len(ctx.pages)
        diag["browser_connected"] = chrome.is_chrome_running()
    except Exception as e:
        diag["browser_error"] = str(e)

    return diag


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    with _sessions_lock:
        active = len(_sessions)
    running = chrome.is_chrome_running()
    data = {
        "status": "ok" if running else "degraded",
        "browser_connected": running,
        "active_sessions": active,
        "max_sessions": MAX_SESSIONS,
    }
    if request.args.get("v") == "1":
        data.update(_get_chrome_diagnostics())
    return jsonify(data)


def _cleanup_expired():
    """Remove expired sessions."""
    with _sessions_lock:
        _evict_expired()


@app.before_request
def _log_request_start():
    global _inflight
    request._start_time = time.time()
    if request.path != "/health":
        # Arm the watchdog for this request. Single-threaded Flask means at most
        # one request executes at a time, so one slot is enough. Grab the URL for
        # the wedge log; get_json caches, so the handler still reads it fine.
        url = ""
        try:
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                url = body.get("url", "") or ""
        except Exception:
            url = ""
        with _inflight_lock:
            _inflight = {
                "path": request.path,
                "url": url,
                "started": request._start_time,
            }
        try:
            chrome.ensure_chrome()
        except Exception as e:
            log.error("Failed to ensure Chrome: %s", e)
            return jsonify({
                "status": "error",
                "error": f"Chrome unavailable: {e}",
            }), 503


@app.teardown_request
def _clear_inflight(_exc=None):
    # Runs after every request, including on exception — so a wedge that unwinds
    # (once the watchdog kills Chrome and the CDP call raises) always clears the
    # slot, and the next request gets a fresh start timestamp.
    global _inflight
    with _inflight_lock:
        _inflight = None


@app.after_request
def _log_request_end(response):
    duration = time.time() - getattr(request, "_start_time", time.time())
    if request.path == "/health" and request.args.get("v") != "1":
        return response
    parts = [
        f"{request.method} {request.path}",
        f"{response.status_code}",
        f"{duration:.1f}s",
    ]
    with _sessions_lock:
        parts.append(f"sessions={len(_sessions)}")
    log.info(" | ".join(parts))
    return response


def _resource_monitor():
    """Background thread: log usage every 30s, evict sessions under pressure."""
    while True:
        time.sleep(30)
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5,
            )
            chrome_rss_kb = 0
            chrome_count = 0
            for line in result.stdout.splitlines():
                if "chrome" in line.lower() and "--type=" in line:
                    chrome_count += 1
                    chrome_rss_kb += int(line.split()[5])
            chrome_rss_mb = chrome_rss_kb // 1024

            container_mb = None
            limit_mb = None
            try:
                with open("/sys/fs/cgroup/memory.current") as f:
                    container_mb = int(f.read().strip()) // (1024 * 1024)
                with open("/sys/fs/cgroup/memory.max") as f:
                    v = f.read().strip()
                    limit_mb = int(v) // (1024 * 1024) if v != "max" else None
            except Exception:
                pass

            pct = (
                round(container_mb / limit_mb * 100, 1)
                if container_mb and limit_mb else 0
            )

            # Active eviction under memory pressure
            if pct > MEMORY_EVICT_PCT:
                with _sessions_lock:
                    if _sessions:
                        oldest = min(
                            _sessions,
                            key=lambda s: _sessions[s]["created_at"],
                        )
                        log.warning(
                            "Memory at %.1f%% — evicting session %s",
                            pct, oldest,
                        )
                        _close_session_unlocked(oldest)

            with _sessions_lock:
                sessions = len(_sessions)

            msg = (
                f"sessions={sessions} "
                f"chrome_procs={chrome_count} chrome_rss={chrome_rss_mb}MB "
                f"container={container_mb}MB/{limit_mb}MB ({pct}%)"
            )
            if pct > MEMORY_EVICT_PCT:
                log.warning("HIGH MEMORY: %s", msg)
            elif pct > 60:
                log.info("monitor: %s", msg)
            else:
                log.debug("monitor: %s", msg)

        except Exception as e:
            log.debug("monitor error: %s", e)


# ---------------------------------------------------------------------------
# Liveness server (separate thread + port)
# ---------------------------------------------------------------------------
#
# The Flask API runs single-threaded (Playwright's sync API uses greenlets that
# can't switch OS threads), so a long in-flight browse blocks every other Flask
# request — including `/health`. The Docker HEALTHCHECK then times out and marks
# a *busy-but-healthy* container `unhealthy`, and the watchdog restarts it
# mid-operation, killing a legitimate session (ISSUE-143, finding 2).
#
# This standalone HTTP server answers `/live` on its own thread and port. A
# Playwright call releases the GIL while it waits on browser I/O, so this thread
# still runs and responds even while Flask is busy — "busy" no longer reads as
# "dead". The cheap `/live` reports unhealthy only when the Chrome *process* is
# actually gone (a non-blocking `poll()`).
#
# `/live?deep=1` adds a second tier: it also probes Chrome's own DevTools
# endpoint, which catches a Chrome whose process is alive but internally wedged
# (hung CDP, deadlocked browser, frozen renderer tree) — the common real-world
# outage `poll()` reports as green (ISSUE-149). A merely-busy browse still passes
# (DevTools answers independently of Flask); a wedged browser does not. The
# launch window is exempt (`is_launching()`): the process exists but DevTools
# isn't up yet, so a relaunch must not read as a wedge. The HEALTHCHECK targets
# the deep tier.

LIVENESS_PORT = int(os.environ.get("BROWSER_LIVENESS_PORT", "9224"))


def _start_liveness_server():
    """Run a tiny liveness HTTP server on its own thread (never blocks)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _LivenessHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            if parsed.path != "/live":
                self.send_response(404)
                self.end_headers()
                return
            deep = parse_qs(parsed.query).get("deep", ["0"])[0] not in (
                "0", "", "false",
            )
            status, body = self._probe(deep)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _probe(deep):
            # Cheap tier: a subprocess poll(), no Playwright/Flask round-trip.
            try:
                alive = chrome.is_chrome_running()
            except Exception:
                alive = False
            if not alive:
                return 503, b"chrome-down\n"
            # Deep tier: is the live process actually responsive? Exempt the
            # launch window (DevTools not up yet) so a relaunch doesn't read as
            # a wedge.
            if deep and not chrome.is_launching() and not chrome.devtools_responding(
                timeout=2,
            ):
                return 503, b"chrome-wedged\n"
            return 200, b"ok\n"

        def log_message(self, *args):  # silence per-request stderr spam
            pass

    server = ThreadingHTTPServer(("0.0.0.0", LIVENESS_PORT), _LivenessHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="liveness")
    t.start()
    log.info("Liveness server listening on :%d/live", LIVENESS_PORT)


# ---------------------------------------------------------------------------
# Browse watchdog (separate thread)
# ---------------------------------------------------------------------------


def _start_browse_watchdog():
    """Kill+relaunch Chrome when a request outlives the hard deadline.

    Catches the renderer/session-level wedge the liveness probe can't see
    (ISSUE-149 / ISSUE-173): DevTools keeps answering so /live?deep=1 stays
    green, but the in-flight /browse never returns and the container is never
    restarted. Runs on its own thread and only ever touches the Chrome OS
    process via recover_wedged_chrome() — never Patchright's thread-bound sync
    objects — so it is safe to fire while the Flask thread is blocked inside a
    CDP call. The kill unblocks that call, so the wedged request fails fast and
    the browser is healed for the next caller.
    """
    if BROWSE_WATCHDOG_DEADLINE_S <= 0:
        log.info("Browse watchdog disabled (BROWSE_WATCHDOG_DEADLINE_S<=0)")
        return

    def _loop():
        last_recovered = 0.0  # start ts of the request we last killed for
        while True:
            time.sleep(BROWSE_WATCHDOG_POLL_S)
            try:
                with _inflight_lock:
                    req = dict(_inflight) if _inflight else None
                if not req:
                    continue
                started = req["started"]
                elapsed = time.time() - started
                if elapsed < BROWSE_WATCHDOG_DEADLINE_S:
                    continue
                if started == last_recovered:
                    continue  # already fired for this request — let it unwind
                last_recovered = started
                log.error(
                    "Browse watchdog: %s %s wedged for %.0fs (deadline %ds) "
                    "— killing+relaunching Chrome",
                    req["path"], req.get("url") or "<no-url>",
                    elapsed, BROWSE_WATCHDOG_DEADLINE_S,
                )
                chrome.recover_wedged_chrome()
                log.info("Browse watchdog: Chrome relaunched after wedge")
            except Exception:
                log.exception("Browse watchdog loop error")

    threading.Thread(target=_loop, daemon=True, name="browse-watchdog").start()
    log.info(
        "Browse watchdog armed (deadline=%ds poll=%ds)",
        BROWSE_WATCHDOG_DEADLINE_S, BROWSE_WATCHDOG_POLL_S,
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

atexit.register(chrome.cleanup)

if __name__ == "__main__":
    chrome.launch_chrome()
    log.info("Chrome launched (pid=%d)", chrome._chrome_proc.pid)
    mon = threading.Thread(target=_resource_monitor, daemon=True)
    mon.start()
    _start_liveness_server()
    _start_browse_watchdog()
    # threaded=False: Playwright sync API uses greenlets that can't
    # switch threads. All requests run on the main thread.
    app.run(host="0.0.0.0", port=9223, threaded=False)
