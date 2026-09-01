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
import render
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

# Set by the resource-monitor thread when memory is over MEMORY_EVICT_PCT, and
# drained by the Flask thread on its next request. The monitor used to evict
# inline, which meant calling _close_session_unlocked() -- and so
# chrome.get_context(), page.goto() and page.close() -- from its own thread.
# Patchright's sync objects are bound to the thread that created them, and
# touching them from another one wedges the process-global asyncio loop for the
# life of the process: every later browse returned a Flask HTML 500 while Chrome
# stayed up and every health probe stayed green (ISSUE-382).
#
# What deferring costs, stated plainly rather than waved away: relief is now
# traffic-gated. A tab that grows on its own -- a running JS timer, a leaking
# page -- was previously freed within 30s by the monitor and is now freed only
# when the next request arrives, so an idle container under pressure rides it
# out and can be OOM-killed by the cgroup instead of shedding a session. That is
# the accepted trade. A kill is loud, bounded and recovered by
# `restart: unless-stopped`; the alternative on offer was a silently poisoned
# process serving 500s for eight hours with every health probe green.
#
# _create_session()'s MEMORY_REJECT_PCT check is a backstop only for the path
# that builds a new session. Requests that supply a session_id never reach it,
# and it frees nothing in any case -- it refuses work. So it does not make the
# paragraph above untrue, and it is not offered as the reason deferring is safe.
#
# An Event rather than a counter: one pressure report asks for one eviction, and
# several reports before the next request must not queue several evictions.
_evict_request = threading.Event()

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

# CDP heartbeat policy — when a run of failed CDP calls counts as a wedge that
# only a container restart can clear (ISSUE-384). chrome.py records the evidence;
# the verdict is here, so tuning how eagerly the container restarts itself does
# not touch the connection code.
#
# Both halves are required, and each rules out a different false positive.
#
# The count, because one failure is not a fault. A single connect_cdp() has
# already retried three times internally, so the default of three consecutive
# failures is nine failed attempts with no success in between -- and any success
# resets it, so a container that is serving anything at all never accumulates.
#
# The window, because a count on its own never expires. Failures stop when
# traffic stops, and without a window a burst at 03:00 would hold the verdict red
# through an idle night and earn a restart for a fault that had already passed.
#
# The window's real constraint is detection latency, not the gap between
# failures. The verdict cannot flicker green between failures -- the count never
# decays and last_failure only ever advances, so once the count crosses the
# threshold the age is measured from the most recent failure and the verdict is
# continuously red. What the window must not be is shorter than the time it takes
# anything to notice: the image HEALTHCHECK is interval=30s retries=3 (about 90s
# to `unhealthy`), and the Ansible watchdog then wants 2 consecutive reads at a
# 1-minute cron (about 120s more). So the floor is roughly 210s, and anything
# below it makes the arm unreachable rather than merely eager. 900s is that floor
# with room, which is why it is the default; CDP_WINDOW_FLOOR_S enforces the rest.
#
# What the pair deliberately does NOT do is treat the absence of a success as a
# fault. An idle container makes no CDP calls for hours and is perfectly healthy;
# only positive evidence of failure counts.
#
# THRESHOLD 0 disables the arm, like BROWSE_WATCHDOG_DEADLINE_S above. The window
# is not an off switch and is clamped rather than honoured at 0, because `age <= 0`
# is false for any real elapsed time -- so a 0 an operator wrote meaning "no
# staleness cutoff" would silently disable the arm, the exact opposite.
CDP_WINDOW_FLOOR_S = 210
CDP_FAILURE_THRESHOLD = int(os.environ.get("BROWSER_CDP_FAILURE_THRESHOLD", "3"))
CDP_FAILURE_WINDOW_S = max(
    CDP_WINDOW_FLOOR_S,
    int(os.environ.get("BROWSER_CDP_FAILURE_WINDOW_S", "900")),
)

# Response budgets. Every one of these is a ceiling a caller may lower, not a
# fixed size: a link-dense hub rendered to markdown legitimately runs past the
# text-extraction defaults these endpoints shipped with (ISSUE-192), and the old
# hard-coded 10k HTML cap on /extract silently cut article bodies in half.
EXTRACT_MAX_CHARS = 25000    # per matched element, per field
EXTRACT_MAX_ELEMENTS = 200
RENDER_MAX_CHARS = 500000


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _read_container_memory_mb():
    """Return (current_mb, limit_mb) from the cgroup, either may be None.

    Split out so the monitor's log line and _get_memory_pct read the same two
    numbers through the same code, and so a test can steer both at once.
    """
    current_mb = None
    limit_mb = None
    try:
        with open("/sys/fs/cgroup/memory.current") as f:
            current_mb = int(f.read().strip()) // (1024 * 1024)
        with open("/sys/fs/cgroup/memory.max") as f:
            v = f.read().strip()
            limit_mb = int(v) // (1024 * 1024) if v != "max" else None
    except Exception:
        pass
    return current_mb, limit_mb


def _get_memory_pct():
    """Return container memory usage percentage, or 0 if unavailable."""
    current_mb, limit_mb = _read_container_memory_mb()
    if current_mb and limit_mb:
        return round(current_mb / limit_mb * 100, 1)
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
            # record=False: this whole block is best-effort teardown behind
            # `except Exception: pass`, and it runs from _cleanup_expired() at
            # the top of every endpoint -- including ones that then return 404
            # without ever making a CDP call of their own, so nothing here can
            # ever produce a compensating success. Counting it let three
            # /interact calls with stale session ids restart the container
            # (ISSUE-384 review).
            ctx = chrome.get_context(record=False)
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


def _note_memory_pressure(pct):
    """Ask the Flask thread for an eviction. Runs on the monitor thread.

    Touches nothing but an Event, by design -- see _evict_request. Returns True
    only on the transition into "requested", so a container sitting above the
    threshold logs once rather than every 30s forever alongside the HIGH MEMORY
    line that already reports the same condition.
    """
    if pct <= MEMORY_EVICT_PCT:
        return False
    if _evict_request.is_set():
        return False
    _evict_request.set()
    return True


def _drain_evict_request_unlocked():
    """Evict the oldest session if the monitor asked for one and memory agrees.

    Flask thread only; caller must hold the lock. Returns the evicted session
    id, or None.

    The memory re-read is what keeps this a deferral rather than a latch. The
    request carries no expiry and only a request clears it, so without the
    re-check a transient spike at 03:00 evicts a live session on the first
    request of the morning at 20% memory — and because every endpoint calls
    _cleanup_expired() *before* _get_session(), the session it evicts can be the
    one that request just named, which comes back as "session not found or
    expired". The old inline eviction could not do that: it only ever ran while
    the condition was true. Two cgroup reads restore that coupling, and it is
    the same cost _create_session() already pays a few lines later.

    The flag is cleared on every path, including when memory has recovered: it
    records "pressure was seen", not "a session is owed". A failed eviction is
    silent, because _close_session_unlocked swallows the Chrome half by design
    (the bookkeeping is popped either way, so the dict stays consistent even
    when the tab survives).
    """
    if not _evict_request.is_set():
        return None
    _evict_request.clear()
    if not _sessions:
        return None
    pct = _get_memory_pct()
    if pct <= MEMORY_EVICT_PCT:
        log.info(
            "Eviction request dropped — memory back to %.1f%% (threshold %d%%)",
            pct, MEMORY_EVICT_PCT,
        )
        return None
    oldest = min(_sessions, key=lambda s: _sessions[s]["created_at"])
    log.warning("Memory at %.1f%% — evicting session %s", pct, oldest)
    _close_session_unlocked(oldest)
    return oldest


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

        content = browsing.extract_page_content(
            page,
            max_chars=data.get("max_chars"),
            max_links=data.get("max_links"),
        )
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
    max_chars = max(1, min(int(data.get("max_chars") or EXTRACT_MAX_CHARS), RENDER_MAX_CHARS))
    limit = max(1, min(int(data.get("limit") or 20), EXTRACT_MAX_ELEMENTS))

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
        for el in elements[:limit]:
            text = el.inner_text().strip()
            html = el.inner_html()
            if text:
                entry = {"text": text[:max_chars], "html": html[:max_chars]}
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


@app.route("/render", methods=["POST"])
def render_page():
    """Render the page to markdown — the structure-preserving read path.

    `/browse` returns flattened text (no hrefs) and a position-stripped anchor
    list, which on an index page reads as nav chrome and article links being the
    same thing. Markdown keeps the heading structure and the href together, so
    the caller can tell them apart without a per-site CSS selector (ISSUE-192).

    `mode=full` serializes the whole page (right for hubs, where the link grid
    *is* the content); `mode=article` isolates the main content first (right for
    article bodies) and degrades to full when the page has no article in it.

    Takes `url` (navigate first), `session_id` (render what that tab already
    holds), or both (navigate within an existing session).
    """
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url")
    session_id = data.get("session_id")
    mode = data.get("mode", "full")
    timeout = data.get("timeout", 30) * 1000
    wait_for = data.get("wait_for")
    keep_session = data.get("keep_session", False)
    skip_behavior = data.get("skip_behavior", False)
    max_chars = max(
        1, min(int(data.get("max_chars") or render.DEFAULT_MAX_CHARS), RENDER_MAX_CHARS),
    )

    created_new = False
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
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        if url:
            _navigate_and_wait(tab_index, url, timeout_ms=timeout)

        chrome.connect_cdp()
        page = _get_page(tab_index)
        if not page:
            raise RuntimeError("Tab not found")

        if url:
            browsing.wait_for_datadome(page)
            if not skip_behavior:
                browsing.simulate_human_behavior(page)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=10000)
                except Exception:
                    pass
            if browsing.detect_captcha(page):
                # Session deliberately left open so the caller can solve it over
                # VNC and retry against the same tab, as /browse does.
                return jsonify({
                    "status": "captcha",
                    "session_id": session_id,
                    "vnc_url": os.environ.get("BROWSER_VNC_URL", ""),
                    "message": "Captcha detected. Solve via VNC, then retry.",
                })

        html = page.content()
        rendered = render.to_markdown(
            html, base_url=page.url, mode=mode, max_chars=max_chars,
        )
        result = {
            "status": "ok",
            "url": page.url,
            "title": page.title(),
            **rendered,
        }

        if keep_session or not created_new:
            result["session_id"] = session_id
        else:
            _close_session(session_id)

        return jsonify(result)

    except Exception as e:
        if created_new and not keep_session:
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
            # record=False: a diagnostics read must not be able to restart the
            # container it is reporting on. is_cdp_connected() stays True across
            # recover_wedged_chrome(), so three /health?v=1 polls taken during a
            # Chrome relaunch would otherwise reach the threshold with no
            # request having been made (ISSUE-384 review).
            ctx = chrome.get_context(record=False)
            diag["browser_pages"] = len(ctx.pages)
        diag["browser_connected"] = chrome.is_chrome_running()
    except Exception as e:
        diag["browser_error"] = str(e)

    return diag


def _cdp_wedged(now=None):
    """Whether the CDP heartbeat shows a live wedge (ISSUE-384).

    Reads chrome.py's record -- a dict copy under a leaf lock -- and touches no
    Patchright machinery, so it is safe from the liveness thread. See the
    CDP_FAILURE_* comment for why both the count and the window are needed.

    The second element is always the full record, on every path including the
    disabled one. One shape, so a caller that indexes it cannot raise: an
    exception here escapes do_GET, the handler answers nothing, `curl -sf`
    fails, and the switch meant to turn this arm off would cause the restart it
    exists to prevent.
    """
    health = chrome.cdp_health()
    if CDP_FAILURE_THRESHOLD <= 0:
        return False, health
    if health["consecutive_failures"] < CDP_FAILURE_THRESHOLD:
        return False, health
    # Monotonic on both sides: chrome.py stamps last_failure with
    # time.monotonic(), so an NTP step cannot move this age.
    age = (now if now is not None else time.monotonic()) - health["last_failure"]
    return age <= CDP_FAILURE_WINDOW_S, health


@app.route("/health", methods=["GET"])
def health():
    """Health check.

    Reports the CDP heartbeat as well as the Chrome process. Through ISSUE-382
    this endpoint returned `status: ok` for eight hours while every browse verb
    500'd, which is what a manual probe gave during the investigation and sent it
    down the wrong path.
    """
    with _sessions_lock:
        active = len(_sessions)
    running = chrome.is_chrome_running()
    wedged, _ = _cdp_wedged()
    # The counters come straight off the record rather than out of _cdp_wedged,
    # so an operator who set the threshold to 0 still sees the evidence here.
    # Switching the arm off means "do not restart the container for this", not
    # "stop reporting it".
    cdp = chrome.cdp_health()
    data = {
        "status": "degraded" if (not running or wedged) else "ok",
        "browser_connected": running,
        "cdp_healthy": not wedged,
        "cdp_consecutive_failures": cdp["consecutive_failures"],
        "cdp_last_error": cdp["last_error"],
        "active_sessions": active,
        "max_sessions": MAX_SESSIONS,
    }
    if request.args.get("v") == "1":
        data.update(_get_chrome_diagnostics())
    return jsonify(data)


def _cleanup_expired():
    """Remove expired sessions, and serve any eviction the monitor asked for.

    Called at the top of every endpoint, so this is where the monitor thread's
    deferred work actually happens -- on the Flask thread, which owns the
    Patchright connection the eviction has to go through.
    """
    with _sessions_lock:
        _evict_expired()
        evicted = _drain_evict_request_unlocked()
    if evicted:
        log.info("Evicted session %s before serving this request", evicted)


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


def _monitor_tick():
    """One pass of the resource monitor: sample usage, log it, request eviction.

    Split out from the loop below so a test can drive it. That is not cosmetic:
    the whole of ISSUE-382 lived in this function body, and while it was inline
    in a `while True: time.sleep(30)` loop nothing could reach it -- so the
    defect could be reintroduced here with the entire suite still green.

    Runs on the monitor thread, and so must touch no Patchright object, directly
    or through a helper. `_note_memory_pressure` is the whole of its interaction
    with session state, by design.
    """
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

    container_mb, limit_mb = _read_container_memory_mb()

    # One source of truth for "what percent are we at". This used to recompute
    # it from the two numbers above, which is a second copy of _get_memory_pct's
    # arithmetic that nothing held equal -- and, since the eviction decision is
    # made from this value and the drain's re-check is made from the other, a
    # divergence would mean the monitor and the Flask thread disagreeing about
    # whether the container is under pressure.
    pct = _get_memory_pct()

    # Request an eviction under memory pressure -- never perform one. Evicting
    # here would reach Patchright from this thread and wedge the process
    # (ISSUE-382); the Flask thread drains it in _cleanup_expired() on the next
    # request.
    if _note_memory_pressure(pct):
        log.warning(
            "Memory at %.1f%% — requesting eviction on the next request", pct,
        )

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


def _resource_monitor():
    """Background thread: log usage every 30s and request eviction under pressure.

    It does not evict. The eviction happens on the Flask thread, in
    _cleanup_expired(); see _evict_request.
    """
    while True:
        time.sleep(30)
        try:
            _monitor_tick()
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
#
# The deep tier has a third arm (ISSUE-384). The first two ask about Chrome; both
# were true for the eight hours of ISSUE-382, when what was dead was this
# process's own Patchright binding. The third reads the CDP heartbeat chrome.py
# publishes — a counter and a timestamp, never a Patchright call from this thread,
# which is the mistake the whole of ISSUE-382 is about.

LIVENESS_PORT = int(os.environ.get("BROWSER_LIVENESS_PORT", "9224"))


# Whether the wedge has already been reported. The liveness thread must not
# block, and logging does: it takes the handler lock and writes to stderr, which
# is a pipe shared with the Flask thread -- the thread that is by hypothesis
# wedged. Logging the transition rather than the state bounds that exposure to
# once per wedge, and it also stops the line repeating every 30s for the rest of
# the day once the watchdog's crash-loop guard has stopped acting on it. A plain
# bool: assignment is atomic under the GIL and no reader needs a consistent pair.
_cdp_wedge_reported = False


def _note_cdp_wedge(wedged, cdp):
    """Log a wedge once when it starts, and once more when it clears."""
    global _cdp_wedge_reported
    if wedged and not _cdp_wedge_reported:
        _cdp_wedge_reported = True
        log.error(
            "Liveness: %d consecutive CDP failures with Chrome up and answering "
            "-- reporting unhealthy so the container is restarted (ISSUE-384). "
            "Last error: %s",
            cdp["consecutive_failures"], cdp["last_error"] or "<none recorded>",
        )
    elif not wedged and _cdp_wedge_reported:
        _cdp_wedge_reported = False
        log.info("Liveness: CDP heartbeat recovered, reporting healthy again")


def _probe(deep):
    """The liveness verdict, as (status, body).

    Module level rather than a method on the handler class so it can be driven
    directly by a test. ISSUE-382's regression was untestable where it lived —
    inside a thread loop — and the first version of its test passed with the bug
    restored; the same trap applies to a probe buried in a nested handler.
    """
    # Cheap tier: a subprocess poll(), no Playwright/Flask round-trip.
    try:
        alive = chrome.is_chrome_running()
    except Exception:
        alive = False
    if not alive:
        return 503, b"chrome-down\n"
    if deep:
        # Deep tier, arm 1: is the live process actually responsive? Exempt the
        # launch window (DevTools not up yet) so a relaunch doesn't read as a
        # wedge.
        if not chrome.is_launching() and not chrome.devtools_responding(timeout=2):
            return 503, b"chrome-wedged\n"
        # Deep tier, arm 2: can this process still drive the browser it is
        # reporting on? Deliberately not exempted by is_launching(): a relaunch
        # explains DevTools being absent for a few seconds and explains nothing
        # about a run of CDP failures that already happened. The browse
        # watchdog's own Chrome restart puts the container in that window, so
        # exempting it would blind the probe for exactly as long as a recovery
        # attempt that cannot fix this fault.
        wedged, cdp = _cdp_wedged()
        _note_cdp_wedge(wedged, cdp)
        if wedged:
            return 503, b"cdp-wedged\n"
    return 200, b"ok\n"


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
            status, body = _probe(deep)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

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
