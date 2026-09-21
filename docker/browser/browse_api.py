"""Browser automation API — Flask endpoints.

Chrome is launched directly (no Patchright ownership) with a stealth
extension for script injection. Patchright connects via CDP only for
content extraction, disconnecting before navigation so Cloudflare
cannot detect an attached debugger.
"""

import atexit
from contextlib import contextmanager
from html import escape
import json
import ipaddress
import logging
import os
import signal
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import quote, urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.local import LocalProxy
from lib.istota_user_scope import scoped_user_dir

import chrome
import pool

import browsing
import render
import visual
import xdotool
from text_budget import checked_offset

# Page helpers resolve the instance from this request, never a shared default.
_user_scope = LocalProxy(lambda: request.user_scope)

def _request_instance():
    # chrome retains instances in its driver-recovery registry. Pass the actual
    # object, never a LocalProxy whose identity changes with the next request.
    return request.browser_instance


app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Session management — a session is one page in its user's Patchright context.
#
# It holds the page *object*, not its index in ctx.pages (ISSUE-535). The index
# was wrong in two directions at once, and both were silent:
#
#   * A tab the page opens -- target="_blank", window.open, a middle click --
#     appends to ctx.pages with no row here. Every reaper iterates _sessions, so
#     nothing could name that tab and nothing ever closed it; closing every
#     session in turn still left it holding a renderer process, and only a
#     watchdog Chrome relaunch cleared it, which throws away the live sessions
#     too. Ownership is now a question the page itself answers, through
#     opener(), so _close_session_unlocked takes a session's popups with it and
#     _sweep_unclaimed_pages_unlocked takes the ones nothing can name at all.
#   * A page closing shifts every index above it. This table was corrected only
#     in _close_session_unlocked, the one closure the API performs itself, so a
#     popup that closed *itself* corrected nothing and every session above it
#     silently addressed its neighbour's tab. Holding the page removes the
#     question rather than adding a second observer for it.
#
# The index survives only where a log line or a response wants a number, derived
# on demand by _tab_index_of. It is authoritative nowhere.
_sessions = {}  # id -> {page, created_at, last_used_at, owner, generation, context}
_sessions_lock = threading.Lock()
SESSION_TTL = 600  # 10 minutes
MAX_SESSIONS = int(os.environ.get("MAX_BROWSER_SESSIONS", "2"))
MAX_TOTAL_SESSIONS = int(os.environ.get("BROWSER_MAX_TOTAL_SESSIONS", "4"))
MEMORY_EVICT_PCT = 80  # evict oldest session above this on non-creation requests
MEMORY_REJECT_PCT = MEMORY_EVICT_PCT  # never add a tab while eviction is needed

# Set by the resource-monitor thread when memory is over MEMORY_EVICT_PCT, and
# drained by the Flask thread on its next request. The monitor used to evict
# inline, which meant calling _close_session_unlocked() -- and so
# chrome.get_context(_request_instance()), page.opener(), page.is_closed(), page.goto() and
# page.close() -- from its own thread. _sweep_unclaimed_pages_unlocked reaches
# the same set and is on the same path. Treat this list as the thread-bound
# surface rather than as illustrative: it is what tells a reader which calls
# may not leave the Flask thread, so a new one belongs in it.
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

# Wedge-recovery policy: when the browse watchdog healing Chrome over and over
# stops being a heal and becomes the fault (ISSUE-394).
#
# The three arms above all ask a question the wedge answers wrongly. Chrome is
# alive, DevTools answers on the browser IO thread, and the CDP heartbeat is not
# only quiet but actively reset -- a hang records nothing, since only a returned
# exception reaches _record_cdp_failure, and the successful connect_cdp() after
# each relaunch zeroes the count. So a container wedging every 90s for hours
# read as healthy on every arm, the watchdog kept healing it, and the only actor
# that ever noticed was a human.
#
# What the container can see is its own recovery rate. One recovery is the
# watchdog working as designed and must not restart anything; a run of them
# inside a window is a browser that cannot stay up, and the process-scoped state
# a relaunch does not clear is exactly what a container restart is for.
#
# The window shares CDP_WINDOW_FLOOR_S for the same reason: below roughly 210s
# the verdict expires before the image HEALTHCHECK (30s x 3) and the Ansible
# watchdog's debounce (2 reads at a 1-minute cron) can act on it, which makes
# the arm unreachable rather than merely eager.
#
# THRESHOLD 0 disables the arm. The default of 3 is deliberately above the two
# recoveries a single bad page can produce back to back.
WEDGE_RECOVERY_THRESHOLD = int(
    os.environ.get("BROWSER_WEDGE_RECOVERY_THRESHOLD", "3"),
)
WEDGE_RECOVERY_WINDOW_S = max(
    CDP_WINDOW_FLOOR_S,
    int(os.environ.get("BROWSER_WEDGE_RECOVERY_WINDOW_S", "900")),
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


class SessionCapacityError(RuntimeError):
    def __init__(self, message, retry_after_seconds):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _capacity_response(error):
    return jsonify({
        "status": "error", "error": str(error),
        "retry_after_seconds": error.retry_after_seconds,
    }), 503


def _create_session(owner=None):
    """Create a new browser tab session.

    Owner is provenance metadata. Capacity is per user, then global; within
    either budget the requesting user's oldest session is replaced first.
    """
    if not isinstance(owner, str) or not owner.strip():
        owner = None
    if MAX_SESSIONS <= 0 or MAX_TOTAL_SESSIONS <= 0:
        raise SessionCapacityError("Browser session capacity is disabled", 30)
    mem_pct = _get_memory_pct()
    if mem_pct > MEMORY_REJECT_PCT:
        raise SessionCapacityError(
            f"Memory pressure too high ({mem_pct}%), refusing new session", 30,
        )

    chrome.connect_cdp(_request_instance())
    ctx = chrome.get_context(_request_instance())

    with _sessions_lock:
        _evict_expired()
        own_sessions = [sid for sid, session in _sessions.items()
                        if session.get("user_id") == _request_instance().user_id]
        while len(own_sessions) >= MAX_SESSIONS or len(_sessions) >= MAX_TOTAL_SESSIONS:
            candidates = own_sessions or list(_sessions)
            if not candidates:
                raise SessionCapacityError("Browser session capacity is disabled", 30)
            oldest = min(candidates, key=lambda sid: _sessions[sid]["last_used_at"])
            _close_session_unlocked(oldest)
            if oldest in own_sessions:
                own_sessions.remove(oldest)

    # The return value, rather than ctx.pages[-1]: the context is shared, so
    # between new_page() and the read a popup can append and the last entry is
    # then somebody else's tab.
    page = ctx.new_page()

    session_id = str(uuid.uuid4())[:8]
    now = time.time()
    with _sessions_lock:
        _sessions[session_id] = {
            "page": page,
            "user_id": _request_instance().user_id,
            "created_at": now,
            "last_used_at": now,
            "owner": owner,
            # Which Chrome this page belongs to. See _get_session.
            "generation": chrome.launch_generation(_request_instance()),
            # And which *connection*, which is not the same question. A page
            # object belongs to the Patchright stack that produced it, and
            # connect_cdp() rebuilds that stack on a failed liveness probe with
            # Chrome still up -- so the generation is unchanged and every
            # wrapper here is silently from a dead connection. The context
            # object is what changes, so it is what identifies the connection.
            "context": ctx,
        }
    return session_id, page


def _get_session(session_id, *, touch=True):
    """Get session info dict, or None if expired, missing, or from a dead Chrome.

    A session holds a page object, which is only meaningful against the Chrome
    that was running when it was created. The browse watchdog kills and
    relaunches Chrome, which comes back with a single about:blank tab while this
    table still holds pages from the dead one -- so every request carrying a
    pre-kill session id resolved to nothing and returned "Tab not found" for the
    rest of the 600s TTL. A recovery that worked, reported to the client as a
    fault, for ten minutes after the fact (ISSUE-394). The generation check is
    still what catches that: a page object from a dead Chrome does not reliably
    report itself closed, so is_closed() below is not a substitute for it.

    Treated as expiry rather than as an error, because that is what it is: the
    tab is gone and the caller's next request opens a fresh one. Dropped here on
    the Flask thread rather than cleared by the watchdog that did the relaunch,
    since touching this table from that thread is a smaller version of the
    mistake ISSUE-382 is about.

    The closed-page arm is the same verdict for the case the index model could
    not see at all: the page itself went -- window.close(), a crashed renderer,
    a tab closed at the noVNC console -- and the session names nothing. Under an
    index that read as somebody else's tab (ISSUE-535).
    """
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None or session.get("user_id") != _user_scope:
            return None
        if pool.instance_for(_user_scope) is None:
            _sessions.pop(session_id, None)
            return None
        now = time.time()
        if now - session["last_used_at"] > SESSION_TTL:
            _close_session_unlocked(session_id)
            return None
        if session.get("generation") != chrome.launch_generation(_request_instance()):
            log.info(
                "Session %s belonged to a previous Chrome (generation %s, now %s) "
                "-- discarding", session_id, session.get("generation"),
                chrome.launch_generation(_request_instance()),
            )
            _sessions.pop(session_id, None)
            return None
        if _page_is_gone(session.get("page")):
            log.info("Session %s's tab is gone -- discarding", session_id)
            _sessions.pop(session_id, None)
            return None
        # The connection, which the generation above does not answer for: a
        # rebuilt Patchright stack leaves this page object bound to a dead one
        # while Chrome, and so the generation, is unchanged. Last, because it is
        # the narrowest of the four and the others give better log lines.
        if session.get("context") is not _request_instance().pw_context:
            log.info(
                "Session %s predates the current CDP connection -- discarding",
                session_id,
            )
            _sessions.pop(session_id, None)
            return None
        if touch:
            session["last_used_at"] = now
        return session


def _page_is_gone(page):
    """Whether this page can still be acted on. Never raises.

    Measured against the shipped image rather than assumed: a wrapper whose CDP
    connection has been torn down answers is_closed() with True, while a real
    call on it (page.title()) raises "Event loop is closed". So the ordinary
    answer here is a plain True and the exception arm is the defensive one --
    but "I could not tell" still has to mean gone, since the alternative is
    handing a dead object to a caller that is about to drive it.

    This is not the guard against a stale *connection*, and must not be read as
    one: it runs per page, and _sweep_unclaimed_pages_unlocked needs to know
    about a rebuilt connection before it closes anything. See the stand-down
    there.
    """
    if page is None:
        return True
    try:
        return page.is_closed()
    except Exception:
        return True


def _opened_by(page, pages):
    """The pages in `pages` that `page` opened, transitively.

    This is the ownership the index model had no way to express: a popup belongs
    to the session whose tab opened it, and Chrome is what knows that.

    **Call this while the opener is still open.** Patchright answers opener()
    with None once the opener has been closed, so closing a session's tab first
    destroys the link to every popup it opened and the walk then finds nothing.
    Measured against the shipped image -- it is the reason this returns a list
    for the caller to close rather than closing as it goes.
    """
    owned = []
    frontier = [page]
    seen = {id(page)}
    while frontier:
        parent = frontier.pop()
        for candidate in pages:
            if id(candidate) in seen:
                continue
            try:
                if candidate.opener() is not parent:
                    continue
            except Exception:
                # A page that will not answer cannot be attributed, and the
                # sweep is what collects it once its opener has gone.
                continue
            seen.add(id(candidate))
            owned.append(candidate)
            frontier.append(candidate)
    return owned


def _close_page(page, ctx):
    """Close one tab, or blank it when it is the last one. Never raises.

    Chrome exits when its final tab closes, taking every other session with it,
    so the last one is navigated to about:blank instead. Best-effort throughout:
    this runs from _cleanup_expired() at the top of every endpoint, and a
    teardown that raised would fail requests over tabs nobody asked about.
    """
    try:
        if len(ctx.pages) <= 1:
            page.goto("about:blank", timeout=5000)
            return
        page.close()
    except Exception:
        pass


def _set_watchdog_instance(inst):
    """Track temporary maintenance targets without changing request identity."""
    with _inflight_lock:
        if _inflight is not None:
            _inflight["instance"] = inst


@contextmanager
def _watchdog_instance(inst):
    with _inflight_lock:
        inflight = _inflight
        previous = inflight.get("instance") if inflight is not None else None
        if inflight is not None:
            inflight["instance"] = inst
    try:
        yield
    finally:
        with _inflight_lock:
            if _inflight is inflight and inflight is not None:
                inflight["instance"] = previous


def _close_session_unlocked(session_id):
    """Close a session, its tab, and every tab that tab opened (caller holds lock).

    The popups go first, and that order is load-bearing rather than tidy: their
    opener() answers None the moment this session's page closes, so a walk done
    afterwards finds nothing to close and the tabs leak exactly as they did
    before (ISSUE-535). They are collected before anything is closed for the
    same reason.
    """
    session = _sessions.pop(session_id, None)
    if not session:
        return
    inst = pool.instance_for(session.get("user_id"))
    if inst is None or session.get("context") is not inst.pw_context:
        return
    with _watchdog_instance(inst):
        page = session.get("page")
        if page is None or not chrome.is_cdp_connected(inst):
            return
        try:
            # record=False: this whole block is best-effort teardown, and it runs
            # from _cleanup_expired() at the top of every endpoint -- including ones
            # that then return 404 without ever making a CDP call of their own, so
            # nothing here can ever produce a compensating success. Counting it let
            # three /interact calls with stale session ids restart the container
            # (ISSUE-384 review).
            ctx = chrome.get_context(inst, record=False)
            owned = _opened_by(page, list(ctx.pages))
        except Exception:
            return
        if owned:
            log.info(
                "Session %s opened %d tab(s) of its own -- closing them with it",
                session_id, len(owned),
            )
        # Deepest first, so a popup is gone before the popup that opened it.
        for extra in reversed(owned):
            _close_page(extra, ctx)
        _close_page(page, ctx)


def _close_session(session_id):
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is not None and session.get("user_id") == _user_scope:
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
    """Evict the least recently used session if the monitor asked for one and memory agrees.

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
    pct = _get_memory_pct()
    if pct <= MEMORY_EVICT_PCT:
        log.info(
            "Eviction request dropped — memory back to %.1f%% (threshold %d%%)",
            pct, MEMORY_EVICT_PCT,
        )
        return None
    victim = pool.evictable(_live_session_users() | {str(_user_scope)})
    if victim is not None:
        with _watchdog_instance(victim):
            pool.release_slot(victim)
        return None
    candidates = [sid for sid, session in _sessions.items()
                  if session.get("user_id") == _user_scope]
    if not candidates:
        return None
    oldest = min(candidates, key=lambda s: _sessions[s]["last_used_at"])
    log.warning("Memory at %.1f%% — evicting session %s", pct, oldest)
    _close_session_unlocked(oldest)
    return oldest


def _live_session_users():
    """Read session liveness without touching Patchright; caller holds lock."""
    now = time.time()
    users = set()
    for session in _sessions.values():
        inst = pool.instance_for(session.get("user_id"))
        if (inst is not None and now - session["last_used_at"] <= SESSION_TTL
                and session.get("generation", inst.launch_generation) == inst.launch_generation
                and session.get("context") is inst.pw_context):
            users.add(inst.user_id)
    return users


def _evict_expired():
    """Remove expired sessions and close their tabs. Caller must hold lock."""
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if now - s["last_used_at"] > SESSION_TTL
    ]
    for sid in expired:
        _close_session_unlocked(sid)


def _sweep_unclaimed_pages_unlocked():
    """Close tabs no session can name. Caller must hold lock. Never raises.

    The backstop behind _close_session_unlocked, and the only thing that clears
    what a deployment has already accumulated: a popup whose opener has gone
    answers opener() with None for ever after, so once its session is closed
    nothing links it to anything and no reaper over _sessions can see it
    (ISSUE-535). Before this, the only thing that cleared one was a watchdog
    Chrome relaunch, which threw away the live sessions with it.

    Three kinds of tab are kept, and the middle one is why this is not
    reap-on-sight:

      * the base tab, which Chrome exits without;
      * a live session's own tab;
      * a tab opened by a live session's tab, transitively -- an OAuth popup
        mid-dance is unreachable through the API and is not garbage, and
        closing it would break the flow the session is in the middle of.

    **How the tab was opened decides whether that middle rule can apply**,
    measured against the shipped image rather than assumed: `window.open`
    (with or without `noopener`) and a `target="_blank"` anchor click both
    report an opener, so those are kept while their session lives. A **middle
    click** reports none, so such a tab is indistinguishable from an orphan and
    is swept on the next request even while its session is live. That is a
    narrowing of the guarantee and not of the leak fix -- no endpoint can
    address such a tab either way.

    Everything else is unreachable by construction: every endpoint resolves a
    page from a session, and no session names these.
    """
    if not chrome.is_cdp_connected(_request_instance()):
        return 0
    try:
        # record=False for the reason _close_session_unlocked gives: this runs
        # at the top of every endpoint and can produce no compensating success.
        ctx = chrome.get_context(_request_instance(), record=False)
        pages = list(ctx.pages)
    except Exception:
        return 0
    if len(pages) <= 1:
        return 0

    live = {id(p) for p in pages}
    keep = {id(pages[0])}
    for session in _sessions.values():
        if session.get("user_id") != _user_scope:
            continue
        page = session.get("page")
        if page is None:
            continue
        if session.get("context") is not ctx:
            # This session was built against a different Patchright stack, so
            # every wrapper it holds is from a connection that has since been
            # rebuilt -- connect_cdp() does that on a failed liveness probe,
            # with Chrome still up and chrome.launch_generation(_request_instance()) unchanged, so
            # _get_session's generation check does not catch it. Those wrappers
            # match nothing in `pages`, so a sweep would find every session's
            # tab unclaimed and close all of them. Stand down for this pass:
            # the rows are dropped by _get_session as endpoints touch them, and
            # the tabs are collected once they are.
            log.info(
                "Sweep stood down: a session predates this CDP connection, so "
                "tab identity cannot be trusted this pass",
            )
            return 0
        if id(page) not in live:
            # Same connection, and the tab is simply not there any more -- it
            # was closed out of band, by the page itself or at the noVNC
            # console. The session names nothing, so it protects nothing; it is
            # dropped by _get_session when an endpoint next asks for it. Not a
            # stand-down: treating it as one blocks the sweep for the whole
            # 600s TTL whenever any tab dies this way, which is ordinary rather
            # than exceptional, and is how the accumulated-orphan case failed.
            continue
        keep.add(id(page))
        for owned in _opened_by(page, pages):
            keep.add(id(owned))

    # A page that will not say who opened it is kept, not closed. _opened_by
    # skips such a candidate, which is right where it is used to *close* a
    # session's own popups -- one it cannot attribute is left for this sweep --
    # and inverted here, where skipping means the page never reaches `keep` and
    # is closed as an orphan. It may be a live session's popup, so the unknown
    # answer has to fail toward keeping it; a genuinely abandoned tab whose
    # opener() is unreadable stays until Chrome is relaunched, which is the leak
    # this sweep exists for and is still better than closing live work.
    for candidate in pages:
        if id(candidate) in keep:
            continue
        try:
            candidate.opener()
        except Exception:
            keep.add(id(candidate))

    unclaimed = [p for p in pages if id(p) not in keep]
    if not unclaimed:
        return 0
    log.warning(
        "Closing %d tab(s) no session can name (%d page(s), %d session(s))",
        len(unclaimed), len(pages), len(_sessions),
    )
    for page in unclaimed:
        _close_page(page, ctx)
    return len(unclaimed)


def _session_page(session):
    """The Patchright page this session named, or None if it is gone.

    Replaces _get_page(tab_index). The rename is deliberate: a fixture stubbing
    the old name as `lambda idx: page` keeps working when handed a session dict
    and silently returns the wrong tab, so changing the argument's meaning under
    the same name would have been a quiet break rather than a loud one.
    """
    if session is None:
        return None
    page = session.get("page")
    return None if _page_is_gone(page) else page


def _tab_index_of(page):
    """Where this page currently sits in the context, or None. Never raises.

    For a log line or a response field, derived at the moment it is read. It is
    not stored, because a stored one goes stale the next time any tab closes --
    which is the whole of ISSUE-535's second half.

    Reads _request_instance().pw_context directly rather than calling get_context(), which
    is what interact() already does for its `others` list. get_context() reaches
    connect_cdp(), which probes the socket with a real round trip and on a stale
    one tears the connection down and rebuilds it with up to two 1s sleeps --
    so formatting a refusal message could invalidate the very page the caller is
    holding and add seconds to the response, with the try/except swallowing any
    sign of it. Both call sites replaced a plain dict lookup, so the cost has to
    stay in that class.
    """
    if page is None:
        return None
    try:
        ctx = _request_instance().pw_context
        if not ctx:
            return None
        for i, candidate in enumerate(ctx.pages):
            if candidate is page:
                return i
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Navigation flow: disconnect CDP -> xdotool -> reconnect CDP
# ---------------------------------------------------------------------------

class NavigationMismatch(RuntimeError):
    def __init__(self, requested, landed):
        super().__init__("navigation_mismatch")
        self.requested = requested
        self.landed = landed


def _document_url(url):
    """Compare document addresses without fragments or browser spelling changes."""
    parts = urlsplit(url)
    if not parts.scheme:
        parts = urlsplit("https://" + url.lstrip("/"))
    if parts.scheme not in ("http", "https"):
        return (url,)
    port = parts.port
    if (parts.scheme, port) in (("https", 443), ("http", 80)):
        port = None
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    # Chrome removes literal and percent-encoded dot segments. Keep empty
    # segments and other escapes: /a//b and /a%2Fb are distinct documents.
    segments = (parts.path or "/").split("/")[1:]
    normalized = []
    for index, segment in enumerate(segments):
        dots = segment.lower().replace("%2e", ".")
        if dots in (".", ".."):
            if dots == ".." and normalized:
                normalized.pop()
            if index == len(segments) - 1:
                normalized.append("")
        else:
            normalized.append(segment)
    path = "/" + "/".join(normalized)
    return (
        parts.scheme, host, port,
        quote(path, safe="/%:@!$&'()*+,;=-._~"),
        quote(parts.query, safe="/%?:@!$&'()*+,;=-._~"),
    )


def _navigation_error_response(error):
    if isinstance(error, NavigationMismatch):
        return jsonify({
            "status": "error", "error": "navigation_mismatch",
            "requested": error.requested, "landed": error.landed,
        }), 502
    return jsonify({"status": "error", "error": str(error)}), 500


def _navigate_and_wait(page, url, timeout_ms=30000):
    """Navigate via xdotool and wait for challenges.

    Chrome was launched with --remote-debugging-port (not --remote-debugging-pipe).
    The port mode doesn't signal an always-attached debugger to Chrome internals,
    unlike the pipe mode which Cloudflare detected. We keep CDP connected for
    simplicity and only use xdotool for navigation input.

    1. Focus the correct tab
    2. Navigate via xdotool (pure X11 keyboard input)
    3. Wait for Cloudflare/security challenges to resolve
    4. Passive wait for page to settle
    5. Require a committed navigation; retry a mismatch once

    Returns the challenge phrase still showing when step 3 ran out of time,
    and None when the page is clear. Every caller has to branch on it: the
    passive wait at step 4 was calibrated for a challenge that clears, and a
    CDP call made while one is still running is the detection vector
    BOT_DETECTION.md documents. `wait_for_datadome` is the first such call in
    every one of them.
    """
    chrome.connect_cdp(_request_instance())

    requested = _document_url(url)
    for attempt in range(2):
        # A pending event from a previous attempt must not prove this one.
        page.wait_for_timeout(1)
        targets = {requested}
        if requested[0] == "http":
            # Chrome may upgrade HTTP before emitting any request event.
            targets.add(("https", *requested[1:]))
        if not urlsplit(url).scheme:
            targets.add(("http", *requested[1:]))
        committed = set()
        arrivals = []
        deadline = time.monotonic() + timeout_ms / 1000

        def on_request(req):
            if not req.is_navigation_request() or req.frame != page.main_frame:
                return
            previous = req.redirected_from
            if previous is not None and _document_url(previous.url) in targets:
                targets.add(_document_url(req.url))

        def on_commit(frame):
            if frame != page.main_frame:
                return
            address = _document_url(frame.url)
            arrivals.append(address)
            # Once the requested document commits, later main-frame commits
            # can be client-side redirects. An iframe cannot vouch for one.
            if address in targets or committed:
                committed.add(address)

        page.on("request", on_request)
        page.on("framenavigated", on_commit)
        try:
            page.bring_to_front()
            xdotool.navigate(url, timeout_s=timeout_ms // 1000, display=_request_instance().display)
            challenge = xdotool.wait_for_challenges(timeout_s=15, display=_request_instance().display)
            time.sleep(browsing.gauss_clamp(3.5, 1.0, 2.0, 5.0))
            if challenge:
                return challenge

            # time.sleep and X11 calls do not dispatch Patchright events.
            # Pump its queue after the passive challenge window, before
            # reading page.url (a cached property), without evaluating JS.
            page.wait_for_timeout(1)
            while not arrivals and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            landed = page.url
            if _document_url(landed) in committed:
                return None
            log.warning(
                "Navigation mismatch on attempt %d: requested=%s landed=%s",
                attempt + 1, url, landed,
            )
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("framenavigated", on_commit)

    raise NavigationMismatch(url, landed)


def _captcha_response(session_id, challenge=None, **extra):
    """The response shape for a challenge requiring operator attention.

    The session is deliberately left open in every case, so the caller can
    request operator help -- or press it with `click_challenge` -- and retry
    against the same tab.

    `challenge` names the title phrase `_navigate_and_wait` saw still showing,
    and is None when `detect_captcha` is what decided. The two are worth
    telling apart: on the title verdict nothing has read the page at all, so
    there is no title, text or link list to be had and none is being withheld.
    The phrase comes from `xdotool.CHALLENGE_TITLE_PATTERNS`, so this quotes
    itself rather than page-controlled text.
    """
    return jsonify({
        "status": "captcha",
        "session_id": session_id,
        "session_retained": True,
        "vnc_url": pool.console_url(_request_instance(), os.environ.get("BROWSER_VNC_URL", "")),
        "instance": {"user": _request_instance().user_id, "slot": _request_instance().slot},
        "message": "Captcha detected. An operator must solve it in the browser console, then retry. The session is retained; close it when finished.",
        "challenge": challenge,
        **extra,
    })


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _checked_url(url):
    """Refuse a url before anything is spent on it, or hand back the clean one.

    navigate() applies the same guard as its first statement, so this is
    never the boundary -- it is where the refusal is cheap. Past this point
    each of the four endpoints has created a session, which is a tab
    launched and then closed, and a refusal that surfaces as the generic
    500 talking about the omnibox where a 400 and no session is the whole
    answer.

    Returns (url, None), or (None, response) for a refusal. A falsy url is
    handed straight back rather than refused here: every caller has its own
    "url or session_id is required" branch, and pre-empting it with a
    different message would change what an empty request answers.
    """
    if not url:
        return url, None
    try:
        return xdotool.literal_url(url), None
    except xdotool.RefusedInput as e:
        return None, (jsonify({"status": "error", "error": str(e)}), 400)


@app.route("/browse", methods=["POST"])
def browse():
    """Navigate to URL and return page content."""
    data = request.get_json()
    try:
        offset = checked_offset(data.get("offset", 0))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    _cleanup_expired(allow_pressure_eviction=bool(data.get("session_id")))
    url = data.get("url", "")
    session_id = data.get("session_id")
    timeout = data.get("timeout", 30) * 1000
    wait_for = data.get("wait_for")
    keep_session = data.get("keep_session", False)
    skip_behavior = data.get("skip_behavior", False)

    if not url and not session_id:
        return jsonify({"error": "url or session_id is required"}), 400

    # Before the session, because a URL this cannot use is a tab that need
    # not be launched -- and because the guard downstream was too late to be
    # one. navigate() types into the omnibox, so the scheme decides what the
    # keystrokes do, and nothing here read it (ISSUE-519's residual). The
    # option shape was refused by xdo_type() three statements into navigate(),
    # after the omnibox had been focused and its contents selected, and came
    # back as a 500 talking about text rather than about a URL (ISSUE-530).
    url, refusal = _checked_url(url)
    if refusal:
        return refusal

    created_new = False
    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        page = _session_page(session)
        if not page:
            # Before navigating, not after. _navigate_and_wait focuses the
            # session's tab and then types the URL over X11, so a None page
            # skips the focus and types into whatever tab is in front -- which
            # is another session's. The _page_is_gone check below the navigation
            # catches a tab that died *during* it; this catches one already gone.
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
    else:
        try:
            session_id, page = _create_session(owner=data.get("owner"))
        except SessionCapacityError as error:
            return _capacity_response(error)
        created_new = True

    try:
        challenge = _navigate_and_wait(page, url, timeout_ms=timeout) if url else None
        if challenge:
            # Answered from the window title, before anything reads the page.
            # Every call below this line goes over CDP -- the DataDome
            # evaluate, detect_captcha's inner_text, wait_for_selector and the
            # content extraction alike -- and a challenge that has not cleared
            # is exactly the window BOT_DETECTION.md says not to do that in.
            return _captcha_response(session_id, challenge)

        # Re-checked after navigating, not just on the way in. _navigate_and_wait
        # can span a watchdog Chrome relaunch, which takes every tab with it --
        # the index model re-resolved here and got None, and holding the object
        # would otherwise carry a dead page straight into the CDP calls below.
        if _page_is_gone(page):
            raise RuntimeError("Tab not found after reconnection")

        if url:
            browsing.wait_for_datadome(page)
        if url and not skip_behavior:
            browsing.simulate_human_behavior(page, display=_request_instance().display)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10000)
            except Exception:
                pass

        if browsing.detect_captcha(page):
            return _captcha_response(session_id)

        content = browsing.extract_page_content(
            page,
            max_chars=data.get("max_chars"),
            max_links=data.get("max_links"),
            offset=offset,
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
        return _navigation_error_response(e)


@app.route("/screenshot", methods=["POST"])
def screenshot():
    """Take a screenshot of the current page.

    A screenshot taken against an existing session also records the coordinate
    frame it was captured in, so a later click_at on that session can be
    converted to an X11 screen point and refused when the page has moved under
    it. A call that supplies a `url` instead creates and closes its own
    session, so it records nothing -- which is why the visual loop uses
    session_id.
    """
    data = request.get_json()
    _cleanup_expired(allow_pressure_eviction=bool(data.get("session_id")))
    url = data.get("url")
    session_id = data.get("session_id")
    full_page = data.get("full_page", False)
    # Skips the one CDP evaluate that reads the page's own url, scroll and
    # viewport. Costs the staleness check its page half; buys a look-and-click
    # loop that sends nothing to the page beyond the capture itself.
    measure = data.get("measure", True)
    timeout = data.get("timeout", 30) * 1000

    url, refusal = _checked_url(url)
    if refusal:
        return refusal

    created_new = False
    page = None
    challenge = None

    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        page = _session_page(session)
    elif url:
        try:
            session_id, page = _create_session(owner=data.get("owner"))
        except SessionCapacityError as error:
            return _capacity_response(error)
        created_new = True
        try:
            challenge = _navigate_and_wait(page, url, timeout_ms=timeout)
            if page:
                # No CDP evaluate while a challenge is still up. Unlike
                # /browse this does not answer `captcha` and return: a
                # screenshot of the interstitial is what the visual path
                # needs in order to press it, so the rest of the endpoint
                # runs and only the documented vector is skipped.
                if not challenge:
                    browsing.wait_for_datadome(page)
                browsing.simulate_human_behavior(page, display=_request_instance().display)
        except Exception as e:
            _close_session(session_id)
            return _navigation_error_response(e)
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        chrome.connect_cdp(_request_instance())
        # _page_is_gone rather than a falsy test: the page object outlives the
        # tab, so a session that survived a watchdog Chrome relaunch holds one
        # that is not None and is not usable either.
        if _page_is_gone(page):
            raise RuntimeError("Tab not found")
        # The capture's own tab switch (ISSUE-536). Every other path that needs
        # the right tab already takes one -- _navigate_and_wait,
        # _coordinate_action, _selector_action -- and this was the only one on
        # the visual path that did not. Under Xvfb with no window manager a tab
        # that is not the foreground tab does not paint, so Playwright waits on
        # a frame that never arrives and the capture dies at its own fixed 30s,
        # on a healthy page that captures in 0.6s the moment it is in front.
        # `--timeout` does not reach it: page.screenshot() takes no timeout
        # argument at all.
        #
        # The second reason is the one that outlives the timeout. This picture
        # is the coordinate frame a later click_at converts against, and X11
        # input acts on whatever tab is in front -- _coordinate_action brings
        # the named tab forward before pressing. A capture taken while some
        # other tab was in front therefore records a frame for a page the
        # pointer was not addressing. Fronting here makes the picture and the
        # pointer agree by construction rather than by coincidence.
        verdict = _capture_foreground(page)
        img_bytes = page.screenshot(full_page=full_page)
        if len(img_bytes) > visual.MAX_SCREENSHOT_BYTES:
            # Refused rather than truncated: a truncated PNG is a corrupt PNG.
            # Only full_page gets anywhere near this, and the remedy is named.
            if created_new:
                _close_session(session_id)
            return jsonify({
                "status": "error",
                "error": (
                    f"screenshot is {len(img_bytes)} bytes, over the "
                    f"{visual.MAX_SCREENSHOT_BYTES} byte cap -- take a viewport "
                    f"capture and scroll instead of full_page"
                ),
            }), 413

        headers = {}
        record, why = visual.build_capture(
            img_bytes, page=page, full_page=full_page,
            # `measure` runs one CDP evaluate for the page's own url, scroll
            # and viewport, which is the same vector as the DataDome evaluate
            # skipped above -- so skipping one and not the other left the
            # documented call going into the live challenge anyway, inside
            # the request whose comment says it does not (ISSUE-531). It
            # costs nothing here: a challenged capture only arrives through
            # the `url` form, which closes its own session, so the page half
            # of the record would serve one response header and be discarded
            # with the session. The X11 half is unaffected and the picture is
            # still clickable against it.
            measure=measure and not challenge,
         display=_request_instance().display)
        if record is None:
            log.info("No capture frame recorded for %s: %s", session_id, why)
            headers["X-Browse-Capture-Error"] = why
        else:
            headers["X-Browse-Capture"] = json.dumps(record)
            if not created_new:
                with _sessions_lock:
                    live = _sessions.get(session_id)
                    if live is not None and live.get("user_id") == _user_scope:
                        live["capture"] = record

        headers.update(_foreground_headers(verdict))

        if created_new:
            _close_session(session_id)
        return Response(img_bytes, mimetype="image/png", headers=headers)
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


# Retained across this user's tabs and session closure, like their profile.
# This is readback redaction, not cleanup of persisted login state.
_credentials_by_user = {}
_credential_values = LocalProxy(lambda: _credentials_by_user.setdefault(str(_user_scope), set()))

_EXTRACT_ELEMENT_JS = """el => {
  const a = n => el.getAttribute(n);
  const entry = {
    text: (el.innerText || '').trim() || a('aria-label') || a('placeholder')
          || a('name') || a('alt') || '',
    html: el.innerHTML,
    tag: el.tagName.toLowerCase(),
  };
  for (const name of ['href', 'src', 'data-link-name', 'id', 'class',
                       'name', 'type', 'alt']) {
    const value = a(name);
    if (value !== null) entry[name] = value;
  }
  if ('value' in el) {
    entry.value_present = String(el.value).length > 0;
    if (el.type !== 'password' && !el.__istotaCredential) entry.value = String(el.value);
  }
  if ('checked' in el) entry.checked = el.checked;
  return entry;
}"""

_PASSWORD_VALUES_JS = """() => Array.from(
  document.querySelectorAll('input[type="password"]'),
  el => [el.value, el.defaultValue]
).flat()"""


def _scrub_extracted(value, secrets):
    if isinstance(value, str):
        variants = set()
        for secret in secrets:
            if secret:
                text_escaped = escape(secret, quote=False)
                # DOM attributes escape double quotes, but keep apostrophes.
                attribute_escaped = text_escaped.replace('"', '&quot;')
                variants.update((secret, escape(secret), text_escaped,
                                 attribute_escaped, quote(secret, safe='')))
        for secret in sorted(variants, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, dict):
        return {key: _scrub_extracted(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_extracted(item, secrets) for item in value]
    return value


@app.route("/extract", methods=["POST"])
def extract():
    """Extract content by CSS selector."""
    data = request.get_json()
    _cleanup_expired(allow_pressure_eviction=bool(data.get("session_id")))
    url = data.get("url")
    session_id = data.get("session_id")
    selector = data.get("selector", "body")
    timeout = data.get("timeout", 30) * 1000
    max_chars = max(1, min(int(data.get("max_chars") or EXTRACT_MAX_CHARS), RENDER_MAX_CHARS))
    limit = max(1, min(int(data.get("limit") or 20), EXTRACT_MAX_ELEMENTS))

    url, refusal = _checked_url(url)
    if refusal:
        return refusal

    created_new = False
    page = None

    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        page = _session_page(session)
    elif url:
        try:
            session_id, page = _create_session(owner=data.get("owner"))
        except SessionCapacityError as error:
            return _capacity_response(error)
        created_new = True
        try:
            challenge = _navigate_and_wait(page, url, timeout_ms=timeout)
            if page:
                # No CDP evaluate while a challenge is still up. This endpoint
                # has no `captcha` shape to answer with, so unlike /browse it
                # carries on and only the documented vector is skipped: the
                # selector reads below still run against whatever the
                # challenge is showing. Giving /extract that shape is a
                # contract change, and is left for whoever wants one.
                if not challenge:
                    browsing.wait_for_datadome(page)
                browsing.simulate_human_behavior(page, display=_request_instance().display)
        except Exception as e:
            _close_session(session_id)
            return _navigation_error_response(e)
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        chrome.connect_cdp(_request_instance())
        # _page_is_gone rather than a falsy test: the page object outlives the
        # tab, so a session that survived a watchdog Chrome relaunch holds one
        # that is not None and is not usable either.
        if _page_is_gone(page):
            raise RuntimeError("Tab not found")

        # Passwords can be autofilled without a credential action. Read their
        # values only into the redactor, never into the returned element data.
        secrets = _credential_values | set(page.evaluate(_PASSWORD_VALUES_JS))
        elements = page.query_selector_all(selector)
        results = []
        for el in elements[:limit]:
            entry = el.evaluate(_EXTRACT_ELEMENT_JS)
            value = entry.get("value", "")
            if any(secret and secret in value for secret in secrets):
                entry.pop("value", None)
            # Scrub before the budgets: truncation can otherwise leave a
            # credential prefix which no longer matches the complete secret.
            entry = _scrub_extracted(entry, secrets)
            for key, value in entry.items():
                if isinstance(value, str):
                    entry[key] = value[:max_chars if key in ("text", "html", "value") else 500]
            results.append(entry)

        if created_new:
            _close_session(session_id)

        return jsonify(_scrub_extracted({
            "status": "ok",
            "url": page.url,
            "selector": selector,
            "count": len(results),
            "elements": results,
        }, secrets))
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify(_scrub_extracted({"status": "error", "error": str(e)},
                                        locals().get("secrets", _credential_values))), 500


# How many frames `include_frames` will actually read. Separate from the
# survey's probe budget because the costs differ in kind: a probe is one small
# CDP round trip, while `frame.content()` pulls a whole document into memory and
# every one of them is then parsed into the same soup. `Frame.content()` takes
# no timeout argument in patchright, so the number of reads is the only bound
# available — there is no per-call one to set.
MAX_FRAME_CONTENT_READS = 10


def _collect_frames(page, include_frames):
    """The page's child frames, as render.to_markdown wants them.

    Returns `(records, capped)`. Every surveyed record is passed through, with
    its `skip` intact, because `to_markdown` reports the nested ones and counts
    only the rest — filtering here is what made a nested frame vanish from a
    census whose own comment said it was counted. `html` is None for a
    content-bearing frame whose content was not asked for or could not be read.

    The survey runs whatever `include_frames` says, because the count is the
    half of ISSUE-516 worth having on its own; only the `frame.content()` reads
    are gated on it, and those are capped again.

    Never raises. A frame walk that fails costs the census, never the render —
    the endpoint's own `except` would otherwise turn a detached frame into a
    500 on a page that rendered perfectly well.
    """
    try:
        records, capped = browsing.survey_frames(page)
    except Exception as e:
        log.warning("frame survey failed: %s", e)
        return [], False

    payload = []
    reads = 0
    for record in records:
        skip = record.get("skip")
        entry = {"url": record.get("url") or "", "skip": skip, "html": None}
        if include_frames and skip is None:
            if reads >= MAX_FRAME_CONTENT_READS:
                log.info("frame content reads capped at %d", MAX_FRAME_CONTENT_READS)
            else:
                reads += 1
                try:
                    entry["html"] = record["frame"].content()
                except Exception as e:
                    log.info(
                        "frame content unreadable (%s): %s", entry["url"], e,
                    )
        payload.append(entry)
    return payload, capped


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

    An iframe's document is a separate frame, so `render` has always dropped it
    and said nothing — a page that is mostly iframe came back `ok` and short
    (ISSUE-516). Every render now carries a `frames` census, and
    `include_frames` splices each surviving frame's content in at its
    `<iframe>`'s own position.

    Takes `url` (navigate first), `session_id` (render what that tab already
    holds), or both (navigate within an existing session).
    """
    data = request.get_json()
    try:
        offset = checked_offset(data.get("offset", 0))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    _cleanup_expired(allow_pressure_eviction=bool(data.get("session_id")))
    url = data.get("url")
    session_id = data.get("session_id")
    include_frames = bool(data.get("include_frames"))
    mode = data.get("mode", "full")
    timeout = data.get("timeout", 30) * 1000
    wait_for = data.get("wait_for")
    keep_session = data.get("keep_session", False)
    skip_behavior = data.get("skip_behavior", False)
    max_chars = max(
        1, min(int(data.get("max_chars") or render.DEFAULT_MAX_CHARS), RENDER_MAX_CHARS),
    )

    url, refusal = _checked_url(url)
    if refusal:
        return refusal

    created_new = False
    if session_id:
        session = _get_session(session_id)
        if not session:
            return jsonify({
                "error": f"session {session_id} not found or expired",
            }), 404
        page = _session_page(session)
    elif url:
        try:
            session_id, page = _create_session(owner=data.get("owner"))
        except SessionCapacityError as error:
            return _capacity_response(error)
        created_new = True
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        if url:
            challenge = _navigate_and_wait(page, url, timeout_ms=timeout)
            if challenge:
                # From the window title, before anything reads the page --
                # page.content() below is as much a CDP call as the DataDome
                # evaluate, and a challenge that has not cleared is the window
                # BOT_DETECTION.md says not to make one in.
                return _captcha_response(session_id, challenge)

        chrome.connect_cdp(_request_instance())
        # _page_is_gone rather than a falsy test: the page object outlives the
        # tab, so a session that survived a watchdog Chrome relaunch holds one
        # that is not None and is not usable either.
        if _page_is_gone(page):
            raise RuntimeError("Tab not found")

        if url:
            browsing.wait_for_datadome(page)
            if not skip_behavior:
                browsing.simulate_human_behavior(page, display=_request_instance().display)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=10000)
                except Exception:
                    pass
            if browsing.detect_captcha(page):
                return _captcha_response(session_id)

        html = page.content()
        frame_payload, frames_capped = _collect_frames(page, include_frames)
        rendered = render.to_markdown(
            html, base_url=page.url, mode=mode, max_chars=max_chars,
            frames=frame_payload, include_frames=include_frames,
            frames_capped=frames_capped, offset=offset,
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
        return _navigation_error_response(e)


# Asked of the module that does the typing rather than written down here.
# It was 4096 against a fixed 30-second timeout that could deliver about a
# quarter of that, and what a caller got past the cap was a field holding
# part of its text, a 500, and the rest of the action list unrun. The number
# is now the inverse of the type timeout's own ceiling, so the two cannot
# disagree again -- see xdotool.TYPE_TIMEOUT_CEILING_S for what bounds it.
MAX_TYPE_CHARS = xdotool.max_type_chars()


def _settle(page, ms):
    """Let the page react to a pointer or key event, swallowing a navigation.

    A visual click is most useful exactly when it navigates -- pressing the
    Cloudflare checkbox replaces the interstitial with the real page -- and a
    navigation destroys the execution context this timer is waiting in.
    Letting that raise loses the result of a click that already happened at
    the X11 level, which is the "success reported as a failure" shape.
    """
    try:
        page.wait_for_timeout(ms)
    except Exception as e:
        log.info("Page changed under the settle wait (this is normal "
                 "after a click that navigates): %s", e)


def _landed_point(screen_x, screen_y):
    """The screen point a press actually reached, for the result to report.

    X11 warps an off-screen request to the nearest addressable pixel, so the
    computed point and the pressed point are the same only while the request
    is in bounds. Reporting the computed one told the caller a click had
    happened somewhere it had not -- the residual ISSUE-530's fix made
    deliberate rather than accidental, and declined at the time because
    reading the pointer back is a round trip per press.

    It is not a round trip any more: xdotool.clamp_to_screen() is arithmetic
    over a memoized screen size, and it answers the same question the warp
    does. Where the geometry is unknown it hands the point back unchanged,
    which is the previous behaviour and the honest one -- an unconfirmed
    clamp is not a confirmed one.
    """
    x, y = xdotool.clamp_to_screen(screen_x, screen_y, display=_request_instance().display)
    return [round(x), round(y)]


def _pointer_refusal(action_type, screen_x, screen_y):
    """The answer when the pointer did not reach the point it was aimed at.

    A move that times out for any reason other than the screen-edge clamp
    leaves the pointer where the previous action put it, and a press there
    lands on an element nobody chose. Reporting that as `ok` with the point
    it was *asked* for is the failure the visual path cannot recover from:
    there is no selector to disconfirm it -- clicking a coordinate is the
    whole point -- so the model reads `ok: true` and reasons about a page
    that was never pressed where it thinks.
    """
    return {
        "action": action_type, "ok": False,
        "error": "pointer_did_not_move",
        "detail": (
            f"the pointer did not reach ({round(screen_x)}, {round(screen_y)}) "
            f"and nothing was pressed; take a fresh screenshot and try again"
        ),
    }


# Which X11 event each direction becomes, on the two paths. Two tables rather
# than one, because the paths address different things: a wheel tick goes to
# whatever the pointer is over and a key goes to whatever holds focus, so the
# direction is the only thing they share.
_WHEEL_BUTTONS = {"up": xdotool.WHEEL_UP, "down": xdotool.WHEEL_DOWN}
_SCROLL_KEYS = {"up": "Page_Up", "down": "Page_Down"}
# The most ticks or presses one action may ask for. Each one is a subprocess
# with its own timeout and a human gap after it, so an unbounded count is an
# action list that never comes back -- and 30 already moves a long page.
MAX_SCROLL_UNITS = 30
SCROLL_SETTLE_MS = 400
# Two defaults, because the two paths move different distances per unit. A
# wheel tick is a detent -- around a tenth of a screen -- and a Page_Down is
# most of one, so one number would be either a twitch or a leap. Only a
# hand-built request reaches these; the skill sends an explicit count.
DEFAULT_WHEEL_CLICKS = 3
DEFAULT_SCROLL_PRESSES = 1


def _bad_direction(action_type, direction):
    return {
        "action": action_type, "ok": False, "error": "bad_direction",
        "detail": (
            f"{direction!r} is not a scroll direction; "
            f"it is up or down"
        ),
    }


def _scroll_count(action_type, value, default):
    """How many ticks or presses this action asks for. Returns (n, refusal).

    Refused rather than clamped. The count comes off model-written JSON, and a
    caller that asked for 500 ticks and silently got 30 has been told a page
    reached the bottom when it did not -- the `ok: true` with something else
    having happened that every refusal on this path exists to prevent. `None`
    is the default rather than a value, since the skill fills it in and a
    hand-built request should not have to.
    """
    if value is None:
        return default, None
    # `bool` is an `int` in Python, and `True` would otherwise be one tick --
    # which is not what a caller who sent it meant by it.
    ok = isinstance(value, int) and not isinstance(value, bool)
    if not ok or not (1 <= value <= MAX_SCROLL_UNITS):
        return None, {
            "action": action_type, "ok": False, "error": "bad_click_count",
            "detail": (
                f"a scroll takes a whole number of wheel ticks or key presses "
                f"between 1 and {MAX_SCROLL_UNITS}"
            ),
        }
    return value, None


def _foreground_tabs(page):
    """The other open tabs, and which of them this session's tab opened.

    Both halves go to the foreground check and they answer different questions.
    `others` is what a title collision is looked for among. `owned` is what
    lets the detail say whose the colliding tab is -- a session's own popup
    carries its opener's title and outlives the action list, so since
    ISSUE-535 that collision is the ordinary case rather than a rare one
    (ISSUE-538). Ownership does **not** take a tab out of `others`; see
    `visual.bring_to_front` for why that would report a confirmed switch in
    exactly the case the title cannot settle.

    Read once per request: an action list is not long enough for the tab set
    to be worth re-reading, and a page that closes under us is caught by the
    try/except around the title read. Never raises -- a context that will not
    answer costs the qualification, not the request.
    """
    try:
        pages = list(_request_instance().pw_context.pages)
    except Exception:
        return [], []
    return [p for p in pages if p is not page], _opened_by(page, pages)


def _foreground(page, action_type, others=(), owned=()):
    """Put the named session's tab in front, or say why the action must not run.

    Returns (verdict, refusal). X11 input reaches whatever tab the one Chrome
    window is showing, and nothing on this path used to bring the named
    session's tab there -- so a coordinate action landed in whichever session
    navigated last, with `ok: true` and the point it was asked for. The
    staleness check cannot see it: it compares the *named* session's url,
    scroll and viewport, none of which moves when the input goes elsewhere.

    Called per action rather than once per request, because a list can
    interleave selector and coordinate actions and because the tab in front is
    not this request's to assume between two of them.
    """
    verdict = visual.bring_to_front(page, others, owned, display=_request_instance().display)
    if verdict.ok:
        if not verdict.confirmed:
            log.info("%s on tab %s: %s", action_type, verdict.code, verdict.detail)
        return verdict, None
    log.info("Refusing %s: %s -- %s", action_type, verdict.code, verdict.detail)
    return verdict, {
        "action": action_type, "ok": False,
        "error": verdict.code, "detail": verdict.detail,
    }


#: Headers a capture reports its tab switch on. A header rather than a field
#: because the body of that response is a PNG. Named beside the capture headers
#: they travel with.
FOREGROUND_HEADER = "X-Browse-Foreground"
FOREGROUND_DETAIL_HEADER = "X-Browse-Foreground-Detail"


def _capture_foreground(page):
    """Bring this page's tab forward for a capture. Returns the verdict.

    Unlike `_foreground`, this one **never refuses**, and that asymmetry is the
    decision ISSUE-536 asked to be made rather than inherited. The coordinate
    path refuses an unconfirmed switch and the selector path falls back to CDP,
    because a click that lands on the wrong tab is invisible to the caller. A
    picture of the wrong tab is not: it is in front of whoever asked for it,
    and refusing the capture would leave them with neither the picture nor a
    way to find out what went wrong. So the capture is taken either way and the
    verdict travels with it.

    `others` is deliberately not passed. It exists only to weaken a verdict to
    `foreground_ambiguous` when another open tab carries the same title, which
    on the coordinate path decides whether to press; here it would cost a
    `title()` round trip per open tab on every capture in the look-and-click
    loop to qualify a note nobody can act on differently.

    Never raises: `visual.bring_to_front` returns a verdict for its own
    failures, and a capture must not be lost to a fault in the thing that was
    only ever meant to improve it.
    """
    try:
        return visual.bring_to_front(page, display=_request_instance().display)
    except Exception as e:  # pragma: no cover - bring_to_front catches its own
        log.info("Capture foreground check failed: %s", e)
        return None


def _foreground_headers(verdict):
    """The capture's foreground verdict as response headers, or {}.

    A confirmed switch is the ordinary case and says nothing, exactly as
    `_with_foreground` adds no key for one -- so a caller seeing no header
    cannot tell a confirmed switch from a container predating this, and does
    not need to: the remedy for both is to look at the picture.
    """
    if verdict is None or verdict.confirmed:
        return {}
    headers = {FOREGROUND_HEADER: verdict.code}
    if verdict.detail:
        headers[FOREGROUND_DETAIL_HEADER] = _header_safe(verdict.detail)
    return headers


def _header_safe(value):
    """A detail string fit for an HTTP header value.

    The detail embeds `page.title()` and `xdotool.window_title()` -- a page's
    own text and a window title derived from it -- so every byte of it is
    chosen by whatever page is loaded. Three separate hazards, and only the
    first is the obvious one:

    * **A line break is a header injection.** `str.split()` removes every one
      Python calls whitespace, which is wider than CRLF: NEL, LS and PS go too.
    * **Anything above U+00FF cannot be sent at all.** The response goes out
      through werkzeug, which encodes each header line `latin-1, strict`, and
      `visual.bring_to_front` builds this string with `{title!r}` -- `repr`
      does not escape non-ASCII printables. So a page (or a sibling tab) whose
      title carries CJK, an emoji or a curly quote raised `UnicodeEncodeError`
      inside `send_header` and the client got a dropped connection: no picture,
      no status, no headers. That is strictly worse than the silence this
      header replaced, and on far commoner input than a newline -- the capture
      was lost in exactly the degraded case the header exists to report.
      `backslashreplace` keeps the title legible as an escape rather than
      dropping it.
    * **A long title is a response nobody wants.** Capped *after* the escape,
      since escaping lengthens and a cap applied first would not bind.

    `unicode_escape` rather than `ascii`/`backslashreplace`, and the difference
    is the third hazard rather than a style: ESC and NUL *are* ASCII, so
    `backslashreplace` has nothing to replace and leaves them in -- they
    survive `str.split()` and werkzeug's CRLF check alike, and go out on the
    wire. `unicode_escape` escapes the C0 set as well as everything above
    U+00FF, so the result is printable ASCII whatever went in. It escapes a
    literal backslash too, which is what keeps the escaping unambiguous.
    """
    collapsed = " ".join(str(value).split())
    escaped = collapsed.encode("unicode_escape").decode("ascii")
    return escaped[:_MAX_HEADER_DETAIL_CHARS]


_MAX_HEADER_DETAIL_CHARS = 300


def _with_foreground(result, verdict):
    """Carry an unconfirmed tab switch into the action's own result.

    Confirmed is the ordinary case and says nothing, so it adds no key. The
    other two are the honest version of "we asked for the tab and could not
    prove we got it", and a caller reading `ok: true` should be able to see
    the difference.
    """
    if verdict is None or verdict.confirmed:
        return result
    return {**result, "foreground": verdict.code, "foreground_detail": verdict.detail}


def _coordinate_action(session, page, action, others=(), owned=()):
    """Run one visual-mode action. Returns the result dict for the action list.

    Every one of these drives X11 rather than CDP. The selector actions beside
    them now do too -- see _selector_action -- but these are the ones with no
    selector to go on, which is the only way to press the Cloudflare
    interstitial's checkbox, whose element lives in a closed shadow root inside
    a cross-origin frame that no selector reaches and whose challenge fails a
    CDP-dispatched click anyway.

    Not all of them have a *point* to go on either. `key`, `type` and the
    keyless `scroll` address whatever holds keyboard focus, so they convert
    nothing and need no capture; the keyless scroll is here rather than beside
    `wait` and `select` in the dispatcher because it is X11 input and takes the
    foreground check every other action on this path takes (ISSUE-528).
    """
    action_type = action["type"]

    # Before the staleness check, deliberately. Staleness asks whether the
    # picture still describes the page; this asks whether the page is the one
    # about to be pressed, and the second question is worthless after the
    # first has passed on a tab nobody is looking at.
    verdict, refusal = _foreground(page, action_type, others, owned)
    if refusal:
        return refusal

    if action_type in ("click_at", "hover_at", "scroll_at", "drag_at"):
        # Both scroll arguments are read before the pointer moves and before
        # the capture is consulted, since the answer depends on the action
        # alone: a refusal after a converted point has travelled has already
        # moved the pointer for an action it then declined.
        if action_type == "scroll_at":
            wheel_button = _WHEEL_BUTTONS.get(action.get("direction", "down"))
            if wheel_button is None:
                return _bad_direction("scroll_at", action.get("direction"))
            clicks, refusal = _scroll_count(
                "scroll_at", action.get("clicks"), DEFAULT_WHEEL_CLICKS)
            if refusal:
                return refusal
            modifier = action.get("modifier")
            if modifier is not None and modifier not in xdotool.MODIFIERS:
                return {
                    "action": "scroll_at", "ok": False,
                    "error": "unknown_modifier",
                    "detail": (
                        f"{modifier!r} is not a modifier this container will "
                        f"hold; it knows {', '.join(xdotool.MODIFIERS)}"
                    ),
                }

        record = session.get("capture")
        code, detail = visual.staleness(record, page, display=_request_instance().display) or (None, None)
        if code:
            log.info("Refusing %s on %s: %s -- %s",
                     action_type, _tab_index_of(page), code, detail)
            return {
                "action": action_type, "ok": False,
                "error": code, "detail": detail,
            }
        try:
            screen_x, screen_y = visual.image_to_screen(
                record, action.get("x"), action.get("y"),
                image_size=action.get("image_size"),
            )
            if action_type == "drag_at":
                end_x, end_y = visual.image_to_screen(
                    record, action.get("to_x"), action.get("to_y"),
                    image_size=action.get("image_size"),
                )
        except ValueError as e:
            return {
                "action": action_type, "ok": False,
                "error": "out_of_picture", "detail": str(e),
            }

        if action_type == "drag_at":
            if not browsing.human_drag_at(screen_x, screen_y, end_x, end_y, display=_request_instance().display):
                return {
                    "action": "drag_at", "ok": False,
                    "error": "drag_incomplete",
                    "detail": "the pointer did not reach a drag endpoint; inspect a new screenshot before retrying",
                }
            _settle(page, 1000)
            return _with_foreground({
                "action": "drag_at", "ok": True,
                "screen": _landed_point(end_x, end_y),
            }, verdict)

        if action_type == "hover_at":
            if not browsing.human_move_to(screen_x, screen_y, display=_request_instance().display):
                return _pointer_refusal("hover_at", screen_x, screen_y)
            return _with_foreground({
                "action": "hover_at", "ok": True,
                "screen": _landed_point(screen_x, screen_y),
            }, verdict)

        if action_type == "scroll_at":
            if not browsing.human_scroll_at(
                screen_x, screen_y, button=wheel_button,
                clicks=clicks, modifier=modifier,
             display=_request_instance().display):
                return _pointer_refusal("scroll_at", screen_x, screen_y)
            # Longer than a key's settle and shorter than a click's: a wheel
            # can start a smooth-scroll animation or a lazy load, and neither
            # is a navigation the click settle is sized for.
            _settle(page, SCROLL_SETTLE_MS)
            result = {
                "action": "scroll_at", "ok": True,
                "direction": action.get("direction", "down"),
                "clicks": clicks,
                "screen": _landed_point(screen_x, screen_y),
            }
            if modifier is not None:
                result["modifier"] = modifier
            return _with_foreground(result, verdict)

        button = 3 if action.get("button") == "right" else 1
        if not browsing.human_click_at(screen_x, screen_y, button=button, display=_request_instance().display):
            return _pointer_refusal("click_at", screen_x, screen_y)
        _settle(page, 1000)
        return _with_foreground({
            "action": "click_at", "ok": True,
            "screen": _landed_point(screen_x, screen_y),
        }, verdict)

    if action_type == "click_challenge":
        # The one action that locates its own target. The checkbox has no
        # selector, so the frame's bounding box plus a measured inset is the
        # only handle on it -- and the frame element does answer bounding_box().
        #
        # It converts through the recorded frame exactly as click_at does, so
        # it takes click_at's staleness pass too. Unreachable today -- under
        # Xvfb with no window manager the frame does not move, and a Chrome
        # relaunch kills the session through the generation check before a
        # stale frame could be used -- but two paths converting against the
        # same record on different evidence is the gap that becomes reachable
        # when something unrelated changes. The codes it can answer with now
        # include `no_coordinate_frame`, which this arm used to report as
        # `no_capture`.
        record = session.get("capture")
        code, detail = visual.staleness(record, page, display=_request_instance().display) or (None, None)
        if code:
            log.info("Refusing click_challenge on %s: %s -- %s",
                     _tab_index_of(page), code, detail)
            return {
                "action": "click_challenge", "ok": False,
                "error": code, "detail": detail,
            }
        point, target = browsing.cloudflare_checkbox_target(page)
        if not point:
            if target == browsing.CF_TARGET_SOLVED:
                # A solved widget keeps its frame at the same 300x65, so
                # geometry still calls it blocking and the checkbox under it is
                # still pressable -- which is what made this action look
                # idempotent when it is not: pressing a green checkbox reports
                # `ok` and starts nothing. `no_challenge` is the wrong word for
                # it, since the caller's next move is to read the page rather
                # than to look for a challenge of another kind (ISSUE-537).
                return {
                    "action": "click_challenge", "ok": False,
                    "error": "challenge_solved",
                    "detail": (
                        "this Cloudflare widget has already been solved -- "
                        "there is nothing to press; read the page instead"
                    ),
                }
            return {
                "action": "click_challenge", "ok": False,
                "error": "no_challenge",
                "detail": "no visible Cloudflare challenge frame on this page",
            }
        screen_x, screen_y = visual.page_to_screen(record, point[0], point[1])
        if not browsing.human_click_at(screen_x, screen_y, display=_request_instance().display):
            return _pointer_refusal("click_challenge", screen_x, screen_y)
        _settle(page, 2000)
        return _with_foreground({
            "action": "click_challenge", "ok": True,
            "css": [round(point[0]), round(point[1])],
            "screen": _landed_point(screen_x, screen_y),
        }, verdict)

    if action_type == "key":
        key = action.get("key", "")
        if not key:
            return {"action": "key", "ok": False, "error": "key is required"}
        try:
            xdotool.key_native(key, display=_request_instance().display)
        except xdotool.OptionShapedInput as e:
            return {
                "action": "key", "ok": False,
                "error": "option_shaped_input", "detail": str(e),
            }
        _settle(page, 300)
        return _with_foreground(
            {"action": "key", "key": key, "ok": True}, verdict)

    if action_type == "type":
        text = action.get("text", "")
        # Refused rather than truncated. Truncation was the old behaviour and
        # it typed most of a value into a live field while reporting `ok`,
        # which a caller cannot undo and has no reason to look for; a refusal
        # leaves the page as it was and says how to chunk. The length check
        # is guarded on the type, since the value comes off model-written
        # JSON and type_native() is what refuses everything else about it.
        if isinstance(text, str) and len(text) > MAX_TYPE_CHARS:
            return {
                "action": "type", "ok": False, "error": "text_too_long",
                "detail": (
                    f"{len(text)} characters is past the {MAX_TYPE_CHARS} one "
                    f"type action can deliver; send it in chunks"
                ),
            }
        try:
            xdotool.type_native(text, display=_request_instance().display)
        except xdotool.OptionShapedInput as e:
            # The refusal reaches the caller as a result rather than as a
            # raise: /interact abandons the rest of its action list on an
            # exception, and a list whose later actions were all fine should
            # not be lost to one argument this one declined.
            return {
                "action": "type", "ok": False,
                "error": "option_shaped_input", "detail": str(e),
            }
        _settle(page, 300)
        return _with_foreground(
            {"action": "type", "chars": len(text), "ok": True}, verdict)

    if action_type == "scroll":
        # No point, so nothing is converted and no capture is needed -- which
        # is what keeps the infinite-scroll recipe working on a session that
        # has never been screenshotted. It still takes the foreground check
        # above, and that is a change the evaluate did not need: a key reaches
        # whatever tab is in front, where `window.scrollBy` reached the named
        # session's page whichever tab that was.
        direction = action.get("direction", "down")
        key = _SCROLL_KEYS.get(direction)
        if key is None:
            return _bad_direction("scroll", direction)
        # The old wire contract carried `amount`, in pixels, and defaulted it
        # to 500 -- so every scroll an older caller sends has it. Reading
        # `presses` and ignoring it would perform one Page_Down and answer
        # `ok: true` for a request that asked to move four screens, which is
        # the "something else happened" shape _scroll_count refuses two
        # screens up. The skill refuses `--scroll-amount` by name; this is the
        # same refusal for a caller that does not go through the skill.
        if "amount" in action:
            return {
                "action": "scroll", "ok": False, "error": "retired_argument",
                "detail": (
                    "`amount` was pixels and no scroll is measured in pixels "
                    "any more; send `presses` (Page_Down presses) or use "
                    "`scroll_at` with `clicks` to turn the wheel at a point"
                ),
            }
        presses, refusal = _scroll_count(
            "scroll", action.get("presses"), DEFAULT_SCROLL_PRESSES)
        if refusal:
            return refusal
        # `key_native` rather than `xdo_key`, which is the one place this
        # departs from what `simulate_human_behavior` does. That one routes
        # through XSendEvent, so its events arrive carrying `send_event=True`;
        # this is model-driven page-level input, which the `key` action beside
        # it already sends through XTest on the stated ground that page input
        # should be indistinguishable from hardware. It also needs no window
        # id, so it has one fewer way to do nothing quietly.
        for _ in range(presses):
            if not xdotool.key_native(key, display=_request_instance().display):
                return {
                    "action": "scroll", "ok": False,
                    "error": "window_not_focused",
                    "detail": (
                        "the Chrome window could not be given X11 focus, so "
                        "the scroll cannot be reported as having moved the "
                        "page; retry, and if it persists the browser is dead"
                    ),
                }
            _settle(page, SCROLL_SETTLE_MS)
        return _with_foreground({
            "action": "scroll", "direction": direction,
            "presses": presses, "ok": True,
        }, verdict)

    # Unreachable while _COORDINATE_ACTIONS and the branches above agree. It
    # answers rather than returning None because the caller appends whatever
    # this gives it, and a null in the action list is worse than a refusal.
    return {"action": action_type, "ok": False, "error": "unknown"}


# --------------------------------------------------------------------------- #
# Selector actions, on the same X11 input the coordinate actions use
# --------------------------------------------------------------------------- #
#
# `page.click` dispatches Input.dispatchMouseEvent and `page.fill` dispatches
# Input.insertText. BOT_DETECTION.md names the first as what DataDome reads
# through screenX/pageX inconsistencies, and it is why the coordinate path was
# built on XTest in the first place. The selector path was the same dispatch,
# on the same pages, reached by the flag a caller is far more likely to use.
#
# `page.fill` is the sharper half: Input.insertText dispatches no keydown and
# no keyup at all, so the value simply appears. A login form behind bot
# detection is exactly where a credential fill gets used, and a password
# appearing in a field with no keystroke history is visible to anything
# watching.
#
# Selector *addressing* does not have to be given up to get X11 input. Resolve
# the element, take its box through DOM.getBoxModel -- which is a read, not an
# Input dispatch -- convert the box centre through the same recorded frame
# click_challenge already uses, and press it with the pointer.
#
# CDP stays for reading, and stays as the fallback. `render`, `extract`,
# `links` and `detect_captcha` all need it; the goal is no CDP *input* on the
# path that has an alternative, not no CDP. Where the X11 path cannot run --
# no coordinate frame, a tab that will not come to the front -- the old call
# still runs and the result says which path it took.

SELECTOR_TIMEOUT_MS = 10000

# Deliberately never reads `el.value`. On a credential fill that is the
# password, and this dict goes into the action result, which is logged by the
# caller and read by a model. Everything here is page structure the caller
# could have read off `extract` anyway.
_ELEMENT_DESC_JS = """el => {
  const a = n => (el.getAttribute ? el.getAttribute(n) : null);
  const label = ((el.innerText || '').trim()
                 || a('aria-label') || a('placeholder') || a('name') || '');
  return {
    tag: el.tagName ? el.tagName.toLowerCase() : null,
    type: a('type'),
    text: label.trim().slice(0, 80),
    href: a('href'),
  };
}"""

_IS_FOCUSED_JS = "el => el === document.activeElement"

_FIELD_HOLDS_JS = (
    "(el, want) => (el.value !== undefined ? el.value : el.textContent) === want"
)


class _Target:
    """Where the pointer has to go to press a resolved element."""

    def __init__(self, handle, css_x, css_y, screen_x, screen_y):
        self.handle = handle
        self.css_x = css_x
        self.css_y = css_y
        self.screen_x = screen_x
        self.screen_y = screen_y


def _describe_element(handle):
    """Tag, type, label and href of the element that was actually matched.

    Worth reporting on its own, and it is what replaces the error the X11 fill
    path gives up: typing at a focused point succeeds whatever the click did,
    so a mis-resolved selector has no `not fillable` to raise. Saying which
    element was pressed is how a caller disconfirms that.
    """
    try:
        return handle.evaluate(_ELEMENT_DESC_JS)
    except Exception as e:
        log.info("Could not describe the matched element: %s", e)
        return None


def _resolve_target(page, selector, frame):
    """Resolve a selector to an X11 point, or (None, (code, detail)).

    `bounding_box()` is viewport-relative, which is what makes this work
    against a frame recorded before the page scrolled: the scroll cancels out
    of both sides, exactly as it does for the iframe box click_challenge
    converts. So this deliberately does not take the staleness pass -- a stale
    *picture* is still a valid coordinate frame, and refusing on it would
    refuse a selector click for a reason that does not apply to it.
    """
    try:
        handle = page.wait_for_selector(
            selector, state="visible", timeout=SELECTOR_TIMEOUT_MS,
        )
    except Exception as e:
        return None, ("no_element",
                      f"no visible element matched {selector!r}: {e}")
    if handle is None:
        return None, ("no_element", f"no visible element matched {selector!r}")

    # page.click does this itself and then re-measures; the X11 path has to do
    # both by hand, and the re-measure is the half that is easy to forget.
    try:
        handle.scroll_into_view_if_needed(timeout=SELECTOR_TIMEOUT_MS)
    except Exception as e:
        return None, ("element_unreachable",
                      f"could not bring {selector!r} into view: {e}")

    box = handle.bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        return None, ("element_not_visible",
                      f"{selector!r} resolved but has no box to press")

    css_x = box["x"] + box["width"] / 2
    css_y = box["y"] + box["height"] / 2

    # An element the scroll could not bring in -- fixed-position furniture
    # overhanging the viewport, a box taller than the window -- would convert
    # to a screen point outside the page and press whatever is there.
    viewport = visual.viewport_size(page)
    if viewport and not (0 <= css_x <= viewport[0] and 0 <= css_y <= viewport[1]):
        return None, ("element_off_screen", (
            f"{selector!r} sits at ({round(css_x)}, {round(css_y)}) which is "
            f"outside the {viewport[0]}x{viewport[1]} viewport even after "
            f"scrolling to it"
        ))

    screen_x, screen_y = visual.page_to_screen(frame, css_x, css_y)
    return _Target(handle, css_x, css_y, screen_x, screen_y), None


def _cdp_selector_action(page, action, path, why):
    """The original CDP call, run because the X11 path could not be.

    Kept rather than deleted, on the issue's own reasoning: `xdotool type` on
    an unusual character set or a non-US layout can mistype where `page.fill`
    is exact, and a login that cannot be completed is worse than one completed
    the detectable way. What changes is that the result says which happened.
    """
    action_type = action["type"]
    selector = action.get("selector", "")
    log.info("Selector %s on %r falling back to CDP: %s",
             action_type, selector, why)
    if action_type == "click":
        page.click(selector, timeout=SELECTOR_TIMEOUT_MS)
        page.wait_for_timeout(1000)
    else:
        page.fill(selector, action.get("value", ""), timeout=SELECTOR_TIMEOUT_MS)
    return {
        "action": action_type, "selector": selector, "ok": True,
        "path": path, "path_reason": why,
    }


def _selector_action(session, page, action, others=(), owned=()):
    """Run one selector action, through the pointer and the keyboard.

    Falls back to the CDP call, reporting that it did, whenever the X11 path
    has no way to run: a tab that will not come to the front, no coordinate
    frame to convert against, a click that did not land on the field, or a
    fill whose keystrokes did not arrive as sent.
    """
    action_type = action["type"]
    if action_type == "fill" and action.get("credential"):
        # Register before either input path, including partially failed fills.
        value = action.get("value", "")
        if value:
            _credential_values.add(value)
    selector = action.get("selector") or ""
    if selector and action_type == "fill" and action.get("credential"):
        # Preserve the fill's wait for dynamically inserted controls. Mark
        # the resolved handle before either input path can write a credential.
        handle = page.wait_for_selector(
            selector, state="visible", timeout=SELECTOR_TIMEOUT_MS,
        )
        handle.evaluate("el => { el.__istotaCredential = true; }")
    if not selector:
        return {"action": action_type, "ok": False,
                "error": "selector is required"}

    # A tab that is not in front takes the pointer and the keyboard to the
    # wrong page. The CDP call addresses the tab directly, so this is a
    # fallback rather than a refusal -- unlike the coordinate path, where
    # there is no correct alternative and the action must not run at all.
    verdict = visual.bring_to_front(page, others, owned, display=_request_instance().display)
    if not verdict.ok:
        return _cdp_selector_action(page, action, "cdp", verdict.detail)

    frame, reason = visual.screen_frame(page, session.get("capture"), display=_request_instance().display)
    if not frame:
        return _cdp_selector_action(page, action, "cdp", reason)

    target, refusal = _resolve_target(page, selector, frame)
    if refusal:
        # Not a fallback: page.click would fail on the same selector for the
        # same reason, and reporting the element problem is more useful than
        # reporting it a second time through another mechanism.
        code, detail = refusal
        return {"action": action_type, "selector": selector, "ok": False,
                "error": code, "detail": detail}

    element = _describe_element(target.handle)

    if action_type == "click":
        button = 3 if action.get("button") == "right" else 1
        if not browsing.human_click_at(
            target.screen_x, target.screen_y, button=button,
         display=_request_instance().display):
            return _pointer_refusal("click", target.screen_x, target.screen_y)
        _settle(page, 1000)
        return _with_foreground({
            "action": "click", "selector": selector, "ok": True,
            "path": "x11",
            "screen": _landed_point(target.screen_x, target.screen_y),
            "element": element,
        }, verdict)

    return _fill_through_keyboard(page, action, target, element, verdict)


def _fill_through_keyboard(page, action, target, element, verdict):
    """Click the field, clear it, and type the value as real keystrokes."""
    selector = action.get("selector", "")
    value = action.get("value", "")
    if not isinstance(value, str):
        return {"action": "fill", "selector": selector, "ok": False,
                "error": "value must be a string"}
    if len(value) > MAX_TYPE_CHARS:
        return {
            "action": "fill", "selector": selector, "ok": False,
            "error": "text_too_long",
            "detail": (f"{len(value)} characters is past the {MAX_TYPE_CHARS} "
                       f"one fill can type"),
        }

    if not browsing.human_click_at(target.screen_x, target.screen_y, display=_request_instance().display):
        return _pointer_refusal("fill", target.screen_x, target.screen_y)

    # The click is what focuses the field, and a click that missed focuses
    # something else -- after which ctrl+a selects the whole page and the
    # value is typed into nothing. Checked before anything is typed, because
    # a credential typed at the wrong focus is the failure this path must not
    # add. `document.activeElement` is read in the handle's own frame.
    try:
        focused = bool(target.handle.evaluate(_IS_FOCUSED_JS))
    except Exception as e:
        log.info("Could not confirm focus on %r: %s", selector, e)
        focused = None
    if focused is False:
        return _cdp_selector_action(
            page, action, "cdp_fallback",
            "the pointer click did not put focus in the field",
        )

    try:
        xdotool.key_native("ctrl+a", display=_request_instance().display)
        xdotool.key_native("Delete", display=_request_instance().display)
        if value:
            xdotool.type_native(value, display=_request_instance().display)
    except xdotool.OptionShapedInput as e:
        return {"action": "fill", "selector": selector, "ok": False,
                "error": "option_shaped_input", "detail": str(e)}

    # `xdotool type` on an unusual character set or a non-US layout can
    # mistype where Input.insertText is exact. The field is asked what it
    # actually holds, and a mismatch takes the CDP path rather than leaving a
    # half-typed credential in a login form reported as `ok`. The comparison
    # runs in the page and its answer is a boolean; the value itself never
    # reaches the result or the log.
    try:
        landed = bool(target.handle.evaluate(_FIELD_HOLDS_JS, value))
    except Exception as e:
        log.info("Could not read back %r after typing: %s", selector, e)
        landed = None
    if landed is False:
        return _cdp_selector_action(
            page, action, "cdp_fallback",
            "the typed value did not arrive in the field as sent",
        )

    result = {
        "action": "fill", "selector": selector, "ok": True,
        "path": "x11",
        "screen": _landed_point(target.screen_x, target.screen_y),
        "element": element,
    }
    if landed is None or focused is None:
        result["verified"] = False
    return _with_foreground(result, verdict)


_SELECTOR_ACTIONS = ("click", "fill")


_COORDINATE_ACTIONS = (
    "click_at", "hover_at", "click_challenge", "key", "type",
    "scroll_at", "scroll", "drag_at",
)


@app.route("/interact", methods=["POST"])
def interact():
    """Interact with an existing session (click, fill, scroll, or a point)."""
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

    chrome.connect_cdp(_request_instance())
    page = _session_page(session)
    if not page:
        return jsonify({"error": "tab not found"}), 500

    others, owned = _foreground_tabs(page)

    results = []
    try:
        for action in actions:
            action_type = action.get("type")
            selector = action.get("selector", "")

            if action_type in _SELECTOR_ACTIONS:
                results.append(
                    _selector_action(session, page, action, others, owned))
            elif action_type == "wait":
                timeout_ms = action.get("timeout", 2000)
                page.wait_for_timeout(min(timeout_ms, 30000))
                results.append({"action": "wait", "ok": True})
            elif action_type == "select":
                # Stays on CDP, and says so. select_option dispatches no
                # Input events at all -- it sets the value and fires
                # input/change through Runtime -- so it is not the
                # Input.dispatchMouseEvent signature the rest of this moved
                # to avoid. Pressing a native <select> through the pointer
                # opens an OS-level popup that is not part of the page and
                # has no box to convert, so there is no X11 path to move it
                # to; what there is, is the escape hatch below.
                value = action.get("value", "")
                page.select_option(selector, value, timeout=10000)
                results.append({
                    "action": "select", "selector": selector, "ok": True,
                    "path": "cdp",
                })
            elif action_type in _COORDINATE_ACTIONS:
                results.append(
                    _coordinate_action(session, page, action, others, owned))
            else:
                results.append({
                    "action": action_type, "ok": False, "error": "unknown",
                })

        if browsing.detect_captcha(page):
            return _captcha_response(session_id, actions=results)

        content = browsing.extract_page_content(page)
        return jsonify({
            "status": "ok",
            "session_id": session_id,
            "actions": results,
            **content,
        })

    except Exception as e:
        # Logged, not just returned. A 500 from here used to leave nothing in
        # the container log at all, so a caller reporting "it failed" and an
        # operator reading the log were looking at two different amounts of
        # information -- and the actions list is empty precisely when the raise
        # beat the append, which is the case the message is needed for.
        log.warning(
            "Interact failed after %d action(s): %s",
            len(results), e, exc_info=True,
        )
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

    chrome.connect_cdp(_request_instance())
    page = _session_page(session)
    if not page:
        return jsonify({"error": "tab not found"}), 500

    try:
        result = page.evaluate(expression)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/challenge", methods=["POST"])
def challenge():
    """Where the captcha/challenge widgets are on this page.

    Geometry rather than a verdict -- detect_captcha() answers whether one is
    there, this answers where. Reports CSS pixels, plus the X11 screen point
    of the Cloudflare checkbox when a capture frame is on record, so a caller
    can see what click_challenge would press before pressing it.
    """
    _cleanup_expired()
    data = request.get_json() or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    session = _get_session(session_id)
    if not session:
        return jsonify({
            "error": f"session {session_id} not found or expired",
        }), 404

    chrome.connect_cdp(_request_instance())
    page = _session_page(session)
    if not page:
        return jsonify({"error": "tab not found"}), 500

    try:
        boxes = browsing.challenge_boxes(page)
        point, target = browsing.cloudflare_checkbox_target(page)
        record = session.get("capture")
        screen = None
        stale = None
        if point and not record:
            # The verb exists to answer "is there a widget, and where", and it
            # used to compute staleness only inside `if point and record` -- so
            # with no capture on record it answered `checkbox_screen: null`
            # with `stale: null`: no point, no reason, and no way to tell "this
            # container cannot convert it" from "there is nothing to convert".
            # `click_challenge` would then refuse with `no_capture`, and
            # nothing before that refusal pointed at the remedy (ISSUE-537).
            code, detail = visual.staleness(record, page, display=_request_instance().display) or (None, None)
            if code:
                stale = {"error": code, "detail": detail}
        elif point and record:
            # The same live re-measure click_challenge performs, for the same
            # reason: this reports the screen point that action would press,
            # and a point converted against a frame that no longer describes
            # the page is a wrong answer given confidently. Reported as
            # `stale` with no point rather than as an error, since the CSS
            # boxes beside it are measured now and are still good.
            code, detail = visual.staleness(record, page, display=_request_instance().display) or (None, None)
            if code:
                stale = {"error": code, "detail": detail}
            else:
                sx, sy = visual.page_to_screen(record, point[0], point[1])
                screen = [round(sx), round(sy)]
        return jsonify({
            "status": "ok",
            "frames": boxes,
            "checkbox_css": [round(point[0]), round(point[1])] if point else None,
            "checkbox_screen": screen,
            "stale": stale,
            # Why there is no point, when there is none. `no_challenge` and
            # `solved` are both "nothing to press" and want opposite things
            # done about them, and the frame list alone cannot separate them --
            # a solved Turnstile is still a visible 300x65 frame on a challenge
            # host. Null whenever a point was produced.
            "checkbox_state": None if point else target,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/sessions/<session_id>", methods=["GET"])
def get_session_info(session_id):
    """Check session status."""
    session = _get_session(session_id)
    if not session:
        return jsonify({"status": "not_found"}), 404
    now = time.time()
    age = now - session["created_at"]
    ttl = max(0, SESSION_TTL - (now - session["last_used_at"]))
    # Try to get URL if CDP is connected
    url = ""
    if chrome.is_cdp_connected(_request_instance()):
        page = _session_page(session)
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
        # The coordinate frame the last screenshot was taken in, or null. A
        # caller that wants to convert a point itself reads it from here; one
        # that sends click_at never needs to.
        "capture": session.get("capture"),
    })


@app.route("/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Close a session."""
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None or session.get("user_id") != _user_scope:
            return jsonify({"status": "not_found"}), 404
    _close_session(session_id)
    return jsonify({"status": "closed", "session_id": session_id})


# ---------------------------------------------------------------------------
# Health and monitoring
# ---------------------------------------------------------------------------

def _read_process_rows():
    """Snapshot process ancestry and RSS without touching browser objects."""
    rows = {}
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,rss=,args="], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            fields = line.split(None, 3)
            if len(fields) == 4:
                try:
                    pid, parent, rss = map(int, fields[:3])
                except ValueError:
                    continue
                rows[pid] = (parent, rss, fields[3])
    except (OSError, subprocess.SubprocessError):
        pass
    return rows


def _process_diagnostics(inst, rows):
    """Attribute Chrome and its descendants to exactly one instance."""
    pids = {inst.proc.pid} if inst.proc is not None else set()
    while True:
        children = {pid for pid, (parent, _, _) in rows.items() if parent in pids}
        if children <= pids:
            break
        pids.update(children)
    detail = []
    for pid in sorted(pids & rows.keys()):
        _, rss, args = rows[pid]
        kind = next((arg.split("=", 1)[1] for arg in args.split()
                     if arg.startswith("--type=")), "browser")
        detail.append({"type": kind, "rss_mb": rss // 1024})
    return {"chrome_processes": len(detail),
            "chrome_rss_mb": sum(rows[pid][1] for pid in pids & rows.keys()) // 1024,
            "process_detail": detail}


def _get_chrome_diagnostics(inst, rows=None):
    """Collect Chrome process and memory diagnostics."""
    diag = {}
    diag.update(_process_diagnostics(inst, _read_process_rows() if rows is None else rows))

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
        diag["chrome_running"] = chrome.is_chrome_running(inst)
        diag["cdp_connected"] = chrome.is_cdp_connected(inst)
        if chrome.is_cdp_connected(inst):
            # record=False: a diagnostics read must not be able to restart the
            # container it is reporting on. is_cdp_connected() stays True across
            # recover_wedged_chrome(), so three /health?v=1 polls taken during a
            # Chrome relaunch would otherwise reach the threshold with no
            # request having been made (ISSUE-384 review).
            ctx = chrome.get_context(inst, record=False)
            diag["browser_pages"] = len(ctx.pages)
        diag["browser_connected"] = chrome.is_chrome_running(inst)
    except Exception as e:
        diag["browser_error"] = str(e)

    return diag


def _cdp_wedged(now=None, inst=None):
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
    health = chrome.cdp_health(inst if inst is not None else _request_instance())
    if CDP_FAILURE_THRESHOLD <= 0:
        return False, health
    if health["consecutive_failures"] < CDP_FAILURE_THRESHOLD:
        return False, health
    # Monotonic on both sides: chrome.py stamps last_failure with
    # time.monotonic(), so an NTP step cannot move this age.
    age = (now if now is not None else time.monotonic()) - health["last_failure"]
    return age <= CDP_FAILURE_WINDOW_S, health


def _wedge_looping(now=None, inst=None):
    """Whether Chrome has been recovered too often lately (ISSUE-394).

    Reads chrome.py's recovery record -- a list copy under a leaf lock, no
    Patchright and no I/O -- so it is safe from the liveness thread.

    Returns (verdict, count_in_window), the same one-shape rule _cdp_wedged
    follows: an exception here escapes do_GET, `curl -sf` fails, and the switch
    meant to turn the arm off would cause the restart it exists to prevent.
    """
    history = chrome.wedge_recovery_history(inst if inst is not None else _request_instance())
    if WEDGE_RECOVERY_THRESHOLD <= 0:
        return False, 0
    # Monotonic on both sides, so an NTP step cannot re-arm an aged-out verdict.
    cutoff = (now if now is not None else time.monotonic()) - WEDGE_RECOVERY_WINDOW_S
    recent = sum(1 for ts in history if ts >= cutoff)
    return recent >= WEDGE_RECOVERY_THRESHOLD, recent


@app.route("/instances", methods=["GET"])
def browser_instances():
    # Management-network metadata, like /health. Never acquire a browser here.
    response = jsonify({"instances": pool.instance_metadata(request.args.get("vnc_url", ""))})
    response.headers["Cache-Control"] = "no-store"
    return response


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
    instances = pool.live()
    running = all(chrome.is_chrome_running(inst) for inst in instances)
    wedged = any(_cdp_wedged(inst=inst)[0] for inst in instances)
    looping = any(_wedge_looping(inst=inst)[0] for inst in instances)
    records = [chrome.cdp_health(inst) for inst in instances]
    process_rows = _read_process_rows()
    data = {
        "status": "degraded" if (not running or wedged or looping) else "ok",
        "per_user_profiles": True,
        "browser_connected": bool(instances) and running,
        "cdp_healthy": not wedged,
        "cdp_consecutive_failures": sum(cdp["consecutive_failures"] for cdp in records),
        "cdp_last_error": next((cdp["last_error"] for cdp in records if cdp["last_error"]), ""),
        "active_sessions": active,
        "total_sessions": active,
        "max_sessions": MAX_SESSIONS,
        "max_total_sessions": MAX_TOTAL_SESSIONS,
        "instances": [{"user": inst.user_id, "slot": inst.slot,
                       "last_used": inst.last_used,
                       **_process_diagnostics(inst, process_rows)} for inst in instances],
    }
    if request.args.get("v") == "1":
        data["instances"] = [
            {"user": inst.user_id, "slot": inst.slot, "last_used": inst.last_used,
             **_get_chrome_diagnostics(inst, process_rows)}
            for inst in instances
        ]
    return jsonify(data)


def _cleanup_expired(allow_pressure_eviction=True):
    """Remove expired sessions, and serve any eviction the monitor asked for.

    Called at the top of every endpoint, so this is where the monitor thread's
    deferred work actually happens -- on the Flask thread, which owns the
    Patchright connection the eviction has to go through.

    The sweep runs between the two. What that buys is **ordering, not relief on
    this request**, and the difference is worth stating because the obvious
    reading is wrong: `page.close()` does not free the renderer synchronously,
    and `_drain_evict_request_unlocked` re-reads the cgroup microseconds later,
    so a request that sheds ten orphans still sees the pre-sweep figure and
    still evicts a live session. What it does deliver is that the orphans are
    gone from then on, so the *next* pressure cycle is measured against a
    browser holding only tabs somebody can name -- which is ISSUE-535's second
    consequence (eviction counted sessions, not tabs, so the tabs that caused
    the pressure were never the ones closed).

    It follows the TTL pass because an expired session's popups are closed by
    _close_session_unlocked itself, so what reaches the sweep from that pass is
    only the remainder: a popup _opened_by could not attribute.
    """
    with _sessions_lock:
        _evict_expired()
        _sweep_unclaimed_pages_unlocked()
        with _watchdog_instance(_request_instance()):
            pool.reap_idle(
                time.monotonic(), exclude=_live_session_users() | {str(_user_scope)},
                on_release=_set_watchdog_instance,
            )
        # A create request must not take a foreign live session as a side
        # effect of cleanup. Creation refuses new work under pressure instead.
        evicted = _drain_evict_request_unlocked() if allow_pressure_eviction else None
    if evicted:
        log.info("Evicted session %s before serving this request", evicted)


def _state_profile():
    return scoped_user_dir(scoped_user_dir(pool.PROFILE_ROOT, "users"), request.user_scope)


def _profile_size(profile):
    """Count regular file bytes without following profile symlinks."""
    def walk_error(error):
        if not isinstance(error, FileNotFoundError):
            raise error

    total = 0
    for directory, _, files in os.walk(profile, followlinks=False, onerror=walk_error):
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except FileNotFoundError:
                continue  # Chrome may replace a cache file during inspection.
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _forget_origin(value):
    """Reject URLs and malformed origins before CDP's empty-origin sentinel."""
    if (not isinstance(value, str) or not value or not value.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or "?" in value or "#" in value):
        raise ValueError("origin must be an http(s) origin without a path, query or credentials")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"}
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)):
        raise ValueError("origin must be an http(s) origin without a path, query or credentials")
    final_label = host.rstrip(".").rsplit(".", 1)[-1]
    if ":" in host or final_label.isdigit() or re.fullmatch(r"0x[0-9a-f]*", final_label):
        host = str(ipaddress.ip_address(host))
    elif (len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in host.rstrip(".").split("."))):
        raise ValueError("origin has an invalid host")
    port = parsed.port  # Raises on invalid and out-of-range ports.
    if parsed.netloc.endswith(":"):
        raise ValueError("origin has an invalid port")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}", host


def _state_sessions():
    """Inspect only this user's sessions on the Flask thread, without renewal.

    Keep the pool's background-safe liveness helper free of Patchright calls.
    State inspection and profile deletion instead use the same page checks as
    a request addressing a session. The Flask server handles requests serially.
    """
    with _sessions_lock:
        ids = [sid for sid, session in _sessions.items()
               if session.get("user_id") == _user_scope]
    inst = request.browser_instance
    if inst is not None and not chrome.is_chrome_running(inst):
        with _sessions_lock:
            for sid in ids:
                _sessions.pop(sid, None)
        return []
    # Drop already-invalid records before asking their connection anything.
    # A watchdog recovery can leave a dead context attached to a new process.
    candidates = [sid for sid in ids if _get_session(sid, touch=False) is not None]
    if candidates:
        # is_closed() reads Patchright's cache. A protocol round trip first
        # delivers pending close events from tabs closed outside this API.
        # Do not reconnect: a cold context must stay cold during inspection.
        inst.pw_context.cookies()
    now = time.time()
    sessions = []
    for sid in candidates:
        session = _get_session(sid, touch=False)
        if session is not None:
            idle = max(0, now - session["last_used_at"])
            sessions.append({
                "session_id": sid,
                "age_seconds": int(max(0, now - session["created_at"])),
                "idle_seconds": int(idle),
                "ttl_seconds": int(max(0, SESSION_TTL - idle)),
            })
    return sessions


@app.route("/state", methods=["GET"])
def browser_state():
    profile = _state_profile()
    if profile is None:
        return jsonify({"status": "error", "error": "user_scope_required"}), 400
    inst = request.browser_instance
    domains = None
    live = inst is not None and chrome.is_chrome_running(inst)
    try:
        sessions = _state_sessions()
        size = _profile_size(profile)
        # Inspection never launches Chrome or reconnects a cold context.
        if live and inst.pw_context is not None:
            domains = sorted({cookie["domain"] for cookie in inst.pw_context.cookies()})
    except Exception:
        return jsonify({"status": "error", "error": "Could not inspect browser state"}), 502
    return jsonify({"status": "ok", "profile_exists": profile.is_dir(),
                    "profile_size_bytes": size, "live": live, "cookie_domains": domains,
                    "sessions": sessions, "session_count": len(sessions)})


@app.route("/state", methods=["DELETE"])
def forget_browser_state():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "error": "Expected a state selection"}), 400
    all_state, whole_profile = body.get("all", False), body.get("profile", False)
    force = body.get("force", False)
    if type(force) is not bool or (force and not (all_state is True and whole_profile is True)):
        return jsonify({"status": "error", "error": "force requires all and profile"}), 400
    origin = body.get("origin")
    if (type(all_state) is not bool or type(whole_profile) is not bool
            or (all_state and origin is not None) or (whole_profile and not all_state)):
        return jsonify({"status": "error", "error": "Select origin or all; profile requires all"}), 400
    host = None
    if not all_state:
        try:
            origin, host = _forget_origin(origin)
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
    profile = _state_profile()
    if profile is None:
        return jsonify({"status": "error", "error": "user_scope_required"}), 400
    inst = request.browser_instance
    if whole_profile:
        try:
            sessions = _state_sessions()
        except Exception:
            return jsonify({"status": "error", "error": "Could not inspect browser sessions"}), 502
        if sessions and not force:
            return jsonify({
                "status": "error", "error": "Close your live browser sessions first, or use --force",
                "sessions": sessions, "session_count": len(sessions),
            }), 409
        closed = []
        try:
            for session in sessions:
                _close_session(session["session_id"])
                closed.append(session["session_id"])
            if inst is not None:
                pool.release_slot(inst, require_stopped=True)
            # Recheck canonical containment after teardown before deleting.
            profile = _state_profile()
            if profile is None:
                return jsonify({"status": "error", "error": "user_scope_required"}), 400
            if profile.exists():
                shutil.rmtree(profile)
        except (OSError, RuntimeError):
            return jsonify({"status": "error", "error": "Could not stop browser or remove profile"}), 502
        return jsonify({"status": "ok", "profile_deleted": True, "closed_sessions": closed})
    if inst is None and not profile.exists():
        return jsonify({"status": "ok", "cleared": "all" if all_state else origin})
    try:
        with _sessions_lock:
            busy_users = _live_session_users()
        inst = pool.acquire(request.user_scope, on_acquire=_track_request_instance,
                            exclude=busy_users, memory_pct=_get_memory_pct,
                            memory_reject_pct=MEMORY_REJECT_PCT)
        chrome.ensure_chrome(inst)
        context = chrome.get_context(inst)
        if all_state:
            context.clear_cookies()
        else:
            domains = {cookie["domain"] for cookie in context.cookies()
                       if host == cookie["domain"].lstrip(".").strip("[]")
                       or host.endswith("." + cookie["domain"].lstrip("."))}
            for domain in sorted(domains):
                context.clear_cookies(domain=domain)
        temporary = not context.pages
        page = context.new_page() if temporary else context.pages[0]
        try:
            cdp = context.new_cdp_session(page)
            try:
                # Chromium treats the empty origin as all origins. Only the
                # explicit all selection may reach that sentinel.
                cdp.send("Storage.clearDataForOrigin", {
                    "origin": "" if all_state else origin,
                    "storageTypes": "local_storage,indexeddb,websql,service_workers,cache_storage",
                })
            finally:
                cdp.detach()
        finally:
            if temporary:
                page.close()
    except (pool.PoolFull, pool.MemoryRejected) as exc:
        return _capacity_response(SessionCapacityError(str(exc), 30))
    except Exception:
        # CDP errors may contain browser data. Report partial failure without it.
        return jsonify({"status": "error", "error": "Browser state clearing failed; some state may already be cleared"}), 502
    return jsonify({"status": "ok", "cleared": "all" if all_state else origin})


def _track_request_instance(inst):
    """Arm recovery before browser work, without publishing a starting instance."""
    global _inflight
    request.browser_instance = inst
    inst.last_used = time.monotonic()
    body = request.get_json(silent=True)
    url = body.get("url", "") if isinstance(body, dict) else ""
    with _inflight_lock:
        _inflight = {
            "path": request.path, "url": url or "",
            "started": request._start_time, "instance": inst,
        }


@app.before_request
def _log_request_start():
    request._start_time = time.time()
    if request.path in {"/health", "/instances"}:
        return None
    user_id = request.headers.get("X-Istota-User")
    # HTTP identity is exact ASCII. Do not normalize or URL-decode it; the
    # canonical scoper alone decides whether the path component is contained.
    if (not isinstance(user_id, str) or not user_id
            or user_id != user_id.strip() or not user_id.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in user_id)
            or scoped_user_dir(scoped_user_dir(pool.PROFILE_ROOT, "users"), user_id) is None):
        return jsonify({"status": "error", "error": "user_scope_required"}), 400
    request.user_scope = user_id
    request.browser_instance = pool.instance_for(user_id)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    session_id = (request.view_args or {}).get("session_id") or body.get("session_id")
    if session_id:
        if not isinstance(session_id, str):
            return jsonify({"error": "invalid session_id"}), 400
        # Refuse before acquire or maintenance: neither a foreign page nor its
        # expiry record may be touched by this caller, even for a missing tab.
        with _sessions_lock:
            session = _sessions.get(session_id)
            if session is not None and session.get("user_id") != user_id:
                return jsonify({"status": "not_found"}), 404
        if request.browser_instance is None and request.endpoint != "delete_session":
            return jsonify({"status": "not_found"}), 404
    if request.browser_instance is not None:
        _track_request_instance(request.browser_instance)
    page_endpoints = {"browse", "render_page", "extract", "screenshot",
                      "interact", "evaluate", "challenge"}
    if request.endpoint not in page_endpoints:
        return None
    try:
        with _sessions_lock:
            busy_users = _live_session_users()
        request.browser_instance = pool.acquire(
            user_id, on_acquire=_track_request_instance, exclude=busy_users,
            memory_pct=_get_memory_pct, memory_reject_pct=MEMORY_REJECT_PCT,
        )
    except (pool.PoolFull, pool.MemoryRejected) as error:
        return _capacity_response(SessionCapacityError(str(error), 30))
    except pool.LaunchFailed as error:
        return jsonify({"status": "error", "error": str(error)}), 502
    try:
        chrome.ensure_chrome(request.browser_instance)
    except Exception as error:
        log.warning("Failed to ensure Chrome: %s", error)
        return jsonify({"status": "error", "error": f"Chrome unavailable: {error}"}), 502


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
    scope = getattr(request, "user_scope", None)
    if scope is not None:
        if response.is_json:
            data = response.get_json()
            if isinstance(data, dict):
                data["user_scope"] = scope
                response.set_data(app.json.dumps(data))
        elif response.mimetype == "image/png" and response.status_code == 200:
            response.headers["X-Istota-User-Scope"] = scope
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
    rows = _read_process_rows()
    instances = pool.live()
    diagnostics = [(inst, _process_diagnostics(inst, rows)) for inst in instances]
    chrome_count = sum(diag["chrome_processes"] for _, diag in diagnostics)
    chrome_rss_mb = sum(diag["chrome_rss_mb"] for _, diag in diagnostics)
    instance_usage = " ".join(
        f"slot={inst.slot}:rss={diag['chrome_rss_mb']}MB" for inst, diag in diagnostics
    )

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
        f"chrome_procs={chrome_count} chrome_rss={chrome_rss_mb}MB {instance_usage} "
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


def _note_cdp_wedge(inst, wedged, cdp):
    """Log a wedge once when it starts, and once more when it clears."""
    if wedged and not inst.cdp_wedge_reported:
        inst.cdp_wedge_reported = True
        log.error(
            "Liveness: %d consecutive CDP failures with Chrome up and answering "
            "-- reporting unhealthy so the container is restarted (ISSUE-384). "
            "Last error: %s",
            cdp["consecutive_failures"], cdp["last_error"] or "<none recorded>",
        )
    elif not wedged and inst.cdp_wedge_reported:
        inst.cdp_wedge_reported = False
        log.info("Liveness: CDP heartbeat recovered, reporting healthy again")


# Same shape and same reason as inst.cdp_wedge_reported above: the liveness thread
# must not block, and logging writes to a stream shared with the Flask thread.


def _note_wedge_loop(inst, looping, recoveries):
    """Log a recovery loop once when it starts, and once more when it clears."""
    if looping and not inst.wedge_loop_reported:
        inst.wedge_loop_reported = True
        log.error(
            "Liveness: %d Chrome wedge recoveries within %ds -- the browse "
            "watchdog is healing the same fault on a loop, reporting unhealthy "
            "so the container is restarted (ISSUE-394)",
            recoveries, WEDGE_RECOVERY_WINDOW_S,
        )
    elif not looping and inst.wedge_loop_reported:
        inst.wedge_loop_reported = False
        log.info("Liveness: wedge recoveries back under threshold, reporting healthy")


def _probe(deep):
    """The liveness verdict, as (status, body).

    Module level rather than a method on the handler class so it can be driven
    directly by a test. ISSUE-382's regression was untestable where it lived —
    inside a thread loop — and the first version of its test passed with the bug
    restored; the same trap applies to a probe buried in a nested handler.
    """
    for inst in pool.live():
        status, body = _probe_instance(inst, deep)
        if status != 200:
            return status, body.rstrip() + f" slot={inst.slot}\n".encode()
    return 200, b"ok\n"


def _probe_instance(inst, deep):
    # Cheap tier: a subprocess poll(), no Playwright/Flask round-trip.
    try:
        alive = chrome.is_chrome_running(inst)
    except Exception:
        alive = False
    if not alive:
        return 503, b"chrome-down\n"
    if deep:
        # Deep tier, arm 1: is the live process actually responsive? Exempt the
        # launch window (DevTools not up yet) so a relaunch doesn't read as a
        # wedge.
        if not chrome.is_launching(inst) and not chrome.devtools_responding(inst, timeout=2):
            return 503, b"chrome-wedged\n"
        # Deep tier, arm 2: can this process still drive the browser it is
        # reporting on? Deliberately not exempted by is_launching(): a relaunch
        # explains DevTools being absent for a few seconds and explains nothing
        # about a run of CDP failures that already happened. The browse
        # watchdog's own Chrome restart puts the container in that window, so
        # exempting it would blind the probe for exactly as long as a recovery
        # attempt that cannot fix this fault.
        wedged, cdp = _cdp_wedged(inst=inst)
        _note_cdp_wedge(inst, wedged, cdp)
        if wedged:
            return 503, b"cdp-wedged\n"
        # Deep tier, arm 3: is this container healing the same wedge on a loop?
        # The three arms above are all satisfied by a Chrome whose UI thread is
        # blocked -- the process lives, DevTools answers on its IO thread, and a
        # hung CDP call records no failure at all. The recovery rate is the one
        # signal the wedge cannot fake, because the watchdog produces it.
        looping, recoveries = _wedge_looping(inst=inst)
        _note_wedge_loop(inst, looping, recoveries)
        if looping:
            return 503, b"wedge-loop\n"
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
                chrome.recover_wedged_chrome(req["instance"])
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

def _exit_on_sigterm(_signum, _frame):
    # Python runs handlers on the main thread. Unwind the request before atexit
    # reaches Patchright, and let a repeated stop signal leave cleanup intact.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


atexit.register(pool.cleanup)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    chrome.migrate_legacy_profile(chrome.PROFILE_ROOT)
    mon = threading.Thread(target=_resource_monitor, daemon=True)
    mon.start()
    _start_liveness_server()
    _start_browse_watchdog()
    # threaded=False: Playwright sync API uses greenlets that can't
    # switch threads. All requests run on the main thread.
    app.run(host="0.0.0.0", port=9223, threaded=False)
