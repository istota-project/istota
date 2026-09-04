"""The signaling supervisor: a watcher per room, and one drain behind them.

Owns the three things the wire protocol deliberately does not: which rooms are
watched, what a decoded event causes, and when either of those is re-decided.
`signaling.py` stays a leaf over the frames — it imports nothing from `istota`
— so everything that needs a config, a database or a `TalkClient` lives here.

**A watcher may only ever join a token the current reconciliation's
`list_conversations` returned.** Never a token from an event payload, never one
from a database row, never one from a task. This is a rule rather than a
consequence, because the scoping property is not the one it is tempting to
write down: `ParticipantService::joinRoom` self-enrols the caller in a
`TYPE_GROUP` or `TYPE_PUBLIC` room that is *listable* to them, and in any
`TYPE_PUBLIC` room as `USER_SELF_JOINED` (`ParticipantService.php:1183-1199`).
So `participants/active` on an arbitrary token is not guaranteed to fail — on a
listable or public room it would quietly make istota a participant. "istota
never joins a room it was not already in" is therefore a property this module
*maintains*, and the way it maintains it is that the only source of tokens is
the bot's own conversation listing. `_may_watch` is that check, and a test
drives it directly.

**Cursor initialisation comes before a watcher, not after.** Catch-up reads
forward from the room's cursor, so a NULL one means reading from zero, which
ingests a room's recent history as new tasks. `reconcile_talk_rooms` already
seeds a cursor from the server's own latest id (`needs_cursor_init`), and
`RoomPass.watchable` is the set of live rooms that came out of that pass with a
cursor. A room whose initialisation failed gets no watcher and is retried on
the next pass. The cost is one pass of latency for a brand-new room's first
message, which arrives instead through the safety-net fetch below.

**The safety net is a comparison, not a sweep.** `RoomPass.behind` is the rooms
whose `lastMessage.id` the listing put ahead of their cursor — free, because
the listing is fetched for the watcher set anyway. On a deployment where the
event stream is working it is empty, so the check issues no fetches at all, and
a non-zero count is the one number that says the stream has silently stopped
delivering while every socket still looks fine. `doctor` reads it as
`rooms_behind`.

**The drain's error contract is part of the coalescing rule rather than an
afterthought.** `poll_one_conversation` wraps the results transaction, whose
documented contract is that it *propagates* — a `create_task` failure rolls the
whole batch back — unlike `_poll_single_conversation`, which swallows fetch
errors. So the in-flight flag is cleared in a `finally` and the dirty bit is
*preserved* on failure, so the next event re-runs rather than coalescing into a
fetch that already died. Clearing in-flight only on the success path strands
that room for the life of the process and nothing notices, because
`talk.signaling_watchers` reports the socket and the socket is fine. A restored
dirty bit deliberately does **not** re-wake the drain: an immediate retry of a
transaction that just raised is a hot loop against Nextcloud. It waits for the
next event or the next reconciliation, and `stats()` carries how many rooms
have been sitting dirty longer than one `room_sync_interval` so the wait is
visible rather than silent.

**Watchers are ordinary child tasks of this coroutine, and that is a decision.**
`AsyncRuntime.spawn` is what schedules the *supervisor*; its watchers are
`asyncio.create_task` children, cancelled and then **awaited** in this
coroutine's `finally`. A `concurrent.futures` handle cannot be awaited — its
`cancel()` resolves out of `PENDING` and fires its callbacks inline while the
task behind it is still winding down (see `AsyncRuntime.spawn`'s docstring) — so
a supervisor that restarted a watcher from a done-callback would start a new one
on top of one still holding its socket and its Talk room session. Nothing here
restarts from a callback: a watcher that ends for good is noticed by the next
reconciliation, which is a bounded, capped restart cadence by construction.

**A stopped watcher leaves its Talk participant session to expire, and that is
a decision.** `join_room_session` POSTs `participants/active` with
`force: true` and nothing here posts the corresponding leave, so a room that
left the listing keeps the bot marked active until Talk reaps the session at
`SESSION_TIMEOUT_KILL` (100 seconds). A leave POST would be a new network call
on the shutdown path, inside `AsyncRuntime.stop`'s budget, that can fail and
must then be swallowed — for a window that closes on its own, in a room the bot
is no longer in. `force: true` supersedes on the way back in, so nothing
depends on the session having gone.

The whole module is best-effort by contract. A push transport that can fail a
task or block ingestion is worse than the poll it replaces, so nothing here
raises into the daemon; the one exception is startup, where the two refusals in
`signaling.py` are called from `run_daemon` before any of this exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from ...config import Config
from . import signaling as sig
from .inbound import (
    RoomContext,
    catch_up_conversation,
    poll_one_conversation,
    reconcile_talk_rooms,
)

logger = logging.getLogger("istota.transport.talk.supervisor")

# How long one settings payload may be reused. The JWT is minted `exp = iat + 60`
# with a minute of leeway either side, so the real lifetime is about two
# minutes — but the point of the cache is not to stretch it, it is that N
# watchers coming back from the hourly ingress drop make **one** settings call
# between them rather than N. The token is per user, not per room.
_SETTINGS_TTL_SECONDS = 30.0

# How long a *failed* settings fetch is shared, the way a successful one is
# (ISSUE-416). The lock was held across the fetch but `_settings_at` was
# stamped only on success, so every waiter released into a failed cache issued
# its own serial fetch — N watchers, N sequential 15s timeouts — which is the
# opposite of what the lock is there for, on the path where it matters most.
# It is much shorter than the positive TTL because it delays a genuine
# recovery: a watcher refused inside the window backs off (1-2s at the first
# attempt) and tries again, so the window only has to be wide enough to
# collapse one burst.
_SETTINGS_FAILURE_TTL_SECONDS = 5.0

# How long a watcher may go without ever once connecting before the supervisor
# treats it as stuck rather than as reconnecting (ISSUE-416). Generous against
# a healthy start — a settings fetch, a `participants/active` POST, a connect,
# a hello, a join and a catch-up, plus a couple of backoff attempts if any of
# them flaps — because the cost of being wrong is cancelling a watcher that was
# about to work. There is no equivalent question for a watcher that *has*
# connected: that one has proved the whole chain works and its reconnect loop
# is the right thing to leave alone.
_NEVER_CONNECTED_SECONDS = 300.0

# A handshake frame that never arrives must not hold a watcher for ever. The
# server's own deadline runs the other way (2s to send `hello` after
# connecting), so this bounds our side of the same exchange.
_HANDSHAKE_TIMEOUT_SECONDS = 15.0

# The reconcile loop wakes at least this often even with a nonsense interval
# configured, so a supervisor cannot become a busy loop on a bad value.
_MIN_SYNC_INTERVAL_SECONDS = 5.0

# A session that lived this long is evidence the deployment works, so the
# reconnect ladder starts again from the bottom. Resetting on *any* successful
# connect is the plausible wrong version: a server that accepts a session and
# drops it immediately would then reconnect every one to two seconds for ever,
# POSTing `participants/active` each time. Resetting on nothing at all is the
# other one, and is what this replaced — the ingress drops every connection
# hourly, so within a day every watcher sat at the ceiling and each of those 24
# daily reconnects cost a 30-60 second inbound blackout instead of one second.
_HEALTHY_SESSION_SECONDS = 60.0

# How long a watcher that stopped for a fatal reason is left alone before it is
# tried again. `WatcherFatal` means "this will not fix itself", so restarting it
# on the reconciliation interval would be the churn the class exists to avoid —
# a settings fetch, a `participants/active` POST and a connect per room every
# five minutes, with `doctor` reporting watchers that are trying. It is a long
# window rather than never, because the operator who fixes `invalid_backend`
# should not also have to restart the daemon.
_FATAL_RETRY_SECONDS = 3600.0


class WatcherFatal(RuntimeError):
    """This watcher will not fix itself; stop it and let reconciliation decide.

    `invalid_backend` is the canonical case: the HPB has no backend configured
    for our Nextcloud URL, and reconnecting on a backoff for ever would leave
    `doctor` reporting a watcher that is trying rather than one that cannot
    work. A second `no_such_room` is the other: Nextcloud answers it for a room
    that is gone, a bot that was removed and a stale Talk session alike, and
    only the third is fixed by a fresh `participants/active`.
    """


@dataclass
class _RoomWatcherState:
    """What survives a reconnect within one watcher, and what does not."""

    resume_id: str | None = None
    talk_session_id: str | None = None
    # Per recovery class, since the last successful hello. `classify_error`
    # turns a code plus this count into "recover" or "fatal", which is what
    # makes the spec's "re-run the join once, guarded against a loop" a
    # mechanism rather than a comment.
    recoveries: dict = field(default_factory=dict)


class RoomWatcher:
    """One WebSocket, one room. Hello, join, catch-up, events, backoff.

    One connection per room is forced by the server rather than chosen:
    `processJoinRoom` calls `session.LeaveRoom(true)` before joining the new
    one (`hub.go:2131`), so a session holds at most one room.

    **`asyncio.CancelledError` is re-raised, never folded into the backoff.**
    A watcher that swallowed it would outlive `AsyncRuntime.stop`'s shutdown
    budget, after which the cleanup hooks close the shared `TalkClient` under a
    request this watcher is still awaiting — the exact ordering that shutdown
    path exists to prevent.
    """

    def __init__(self, supervisor: "SignalingSupervisor", token: str) -> None:
        self._sup = supervisor
        self.token = token
        self.connected = False
        # **A different question from `connected`, and the one nothing could
        # ask** (ISSUE-416). `connected` is "right now"; this is "ever, since
        # this watcher was constructed". Without it a watcher wedged before it
        # ever reached a socket is indistinguishable from one that connected an
        # hour ago and is briefly reconnecting — for ever, in both
        # `_start_missing` and `doctor` — which is why ISSUE-414 was invisible
        # from outside while one room sat dark.
        self.ever_connected = False
        self.started_at = time.monotonic()
        # Set when `run` returns because the watcher will not fix itself. The
        # supervisor reads it rather than inferring from `task.done()`, which
        # cannot tell a fatal stop from any other kind of ending.
        self.fatal = False
        self._state = _RoomWatcherState()
        self._frame_seq = 0
        self._connected_at = 0.0

    # -- frame plumbing ----------------------------------------------------

    def _next_id(self) -> str:
        self._frame_seq += 1
        return str(self._frame_seq)

    async def _send(self, ws, frame: dict) -> None:
        # Never logged. A hello carries the JWT or the v1 ticket and a resume
        # carries the `resumeid`, each of which authenticates a full session.
        await ws.send(json.dumps(frame))

    async def _recv(self, ws, *, timeout: float | None = None):
        raw = await (
            asyncio.wait_for(ws.recv(), timeout=timeout)
            if timeout is not None else ws.recv()
        )
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            # Total by contract, like `parse_event`: a server we do not control
            # decides what arrives, and an exception here would leave the room
            # unwatched until the next reconciliation. Never the payload — on
            # the relay path it is somebody's chat.
            self._sup.count("unreadable_frames")
            return None

    # -- one connection ----------------------------------------------------

    async def run(self) -> None:
        attempt = 0
        while True:
            self._connected_at = 0.0
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except WatcherFatal as e:
                logger.error(
                    "Talk signaling watcher for %s stopped: %s", self.token, e,
                )
                self.fatal = True
                self._sup.count("watchers_stopped")
                return
            except Exception as e:  # noqa: BLE001 — a watcher never raises out
                logger.warning(
                    "Talk signaling watcher for %s disconnected: %s: %s",
                    self.token, type(e).__name__, e,
                )
            finally:
                self.connected = False

            # The ladder resets on evidence that the session *worked*, never on
            # how it ended. `_session` has no normal return — `_consume` loops
            # until something raises, and a clean socket close arrives as
            # `ConnectionClosedOK` — so a reset placed on the non-exception
            # path is unreachable, which is what this replaced.
            if (
                self._connected_at
                and time.monotonic() - self._connected_at >= _HEALTHY_SESSION_SECONDS
            ):
                attempt = 0

            delay = sig.backoff_delay(
                attempt,
                maximum=self._sup.config.talk.signaling.reconnect_backoff_max,
            )
            attempt += 1
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        settings = await self._sup.settings()
        url = sig.websocket_url(self._sup.signaling_url(settings))

        async with self._sup.connect(url) as ws:
            welcome = await self._recv(ws, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            features = sig.parse_welcome(welcome)
            self._sup.note_features(features)

            await self._handshake(ws, settings, features)
            await self._join(ws)

            self.connected = True
            self.ever_connected = True
            self._connected_at = time.monotonic()
            self._state.recoveries.clear()

            # Always, resume or not. A resumed session replays only what the
            # server buffered during the gap (30s, `hub.go:119`), and treating
            # that as complete coverage is how a silent gap is produced.
            await self._sup.catch_up(self.token)

            await self._consume(ws)

    async def _handshake(self, ws, settings, features) -> None:
        """Resume if we can, else authenticate. Bounded, and never silent."""
        while True:
            if self._state.resume_id:
                await self._send(
                    ws, sig.build_resume(self._state.resume_id, self._next_id()),
                )
            else:
                await self._send(
                    ws, sig.build_hello(settings, features, self._next_id()),
                )

            frame = await self._recv(ws, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            error = sig.parse_error(frame)
            if error is None:
                hello = (frame or {}).get("hello")
                if not isinstance(hello, dict):
                    raise WatcherFatal(
                        "signaling hello was answered with neither a session "
                        "nor an error"
                    )
                # The public session id is safe at debug and is what makes a
                # connection traceable; `resumeid` is a bearer credential for
                # the 30-second resume window and is held in memory only.
                self._state.resume_id = hello.get("resumeid") or None
                logger.debug(
                    "Talk signaling session %s open for room %s",
                    hello.get("sessionid"), self.token,
                )
                return

            recovery = self._recover(error)
            if recovery == sig.RECOVERY_FRESH_HELLO:
                # The resume was refused, which past 30 seconds is the expected
                # answer rather than a fault.
                self._state.resume_id = None
                continue
            if recovery == sig.RECOVERY_FRESH_TOKEN:
                if sig.is_clock_skew(error.code):
                    logger.warning(
                        "Talk signaling refused our token as not-yet-valid: the "
                        "Nextcloud host and the signaling host disagree about "
                        "the time by more than a minute of leeway. Re-fetching "
                        "mints a token with a later iat, so this is an operator "
                        "problem rather than something a retry fixes.",
                    )
                settings = await self._sup.settings(discard=settings)
                self._state.resume_id = None
                continue
            raise WatcherFatal(f"hello refused: {error}")

    async def _join(self, ws) -> None:
        """`participants/active`, then the room frame. One retry, then stop.

        `no_such_room` is the normal steady-state failure and is deliberately
        not diagnosable: Nextcloud returns one error for a room that is gone, a
        user who is not a participant and a stale Talk session id, so that room
        existence does not leak (`SignalingController.php:907-908`). After an
        outage longer than the Talk session timeout the third is the likely
        one, and the fix is a fresh session rather than a fresh token.
        """
        while True:
            self._state.talk_session_id = await self._sup.join_room_session(
                self.token,
            )
            await self._send(ws, sig.build_room_join(
                self.token, self._state.talk_session_id, self._next_id(),
            ))

            frame = await self._recv(ws, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            error = sig.parse_error(frame)
            if error is None:
                return

            if self._recover(error) == sig.RECOVERY_FRESH_SESSION:
                logger.info(
                    "Talk signaling join for %s refused; retrying with a fresh "
                    "Talk session", self.token,
                )
                continue
            raise WatcherFatal(f"room join refused: {error}")

    def _recover(self, error) -> str:
        """This code's recovery class, with this connection's budget applied."""
        first = sig.classify_error(error.code)
        taken = self._state.recoveries.get(first, 0)
        recovery = sig.classify_error(error.code, attempt=taken)
        self._state.recoveries[first] = taken + 1
        return recovery

    async def _consume(self, ws) -> None:
        while True:
            frame = await self._recv(ws)
            if frame is None:
                continue

            error = sig.parse_error(frame)
            if error is not None:
                if self._recover(error) == sig.RECOVERY_FATAL:
                    raise WatcherFatal(f"session refused: {error}")
                # Anything recoverable is recovered by reconnecting, which is
                # the one path that re-runs every step in order.
                raise RuntimeError(f"session interrupted: {error}")

            event = sig.parse_event(frame)
            if event is None:
                self._sup.count("ignored_frames")
                continue

            self._sup.count(
                "refresh_events" if event.refresh_only else "comment_events",
            )
            # Trigger mode: the payload is not read. A relayed comment and a
            # bare refresh are the same instruction — fetch this room — which
            # is what makes this path unable to be wrong about message content.
            self._sup.mark_dirty(event.room_token, watcher_token=self.token)


class SignalingSupervisor:
    """The watcher set, the reconciliation task and the coalescing fetch queue.

    `connect` and `client_factory` are injectable so the whole of this is
    drivable in a unit test with no socket and no Nextcloud; the defaults are
    the real ones.
    """

    def __init__(
        self,
        config: Config,
        *,
        connect=None,
        client_factory=None,
    ) -> None:
        self.config = config
        self._connect = connect
        self._client_factory = client_factory

        self._watchers: dict[str, RoomWatcher] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._context: dict[str, RoomContext] = {}
        self._live: set[str] = set()
        self._watchable: set[str] = set()

        # token -> monotonic stamp of the *first* event that made it dirty, so
        # the age below means "how long this room has been owed a fetch".
        self._dirty: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

        self._rooms_behind = 0
        # token -> the monotonic time before which a fatally-stopped watcher is
        # not tried again.
        self._fatal_until: dict[str, float] = {}
        self._counters: dict[str, int] = {}
        self._settings = None
        self._settings_at = 0.0
        # The negative half of the same cache: the last settings failure and
        # when it happened, so a burst of waiters shares one failure instead of
        # each paying for its own. Deliberately *not* folded into
        # `_settings_at`, which means "when `_settings` was fetched" — stamping
        # that on a failure would make a stale successful payload look fresh,
        # and the token it carries lives about two minutes.
        self._settings_error: BaseException | None = None
        self._settings_error_at = 0.0
        self._settings_lock = asyncio.Lock()
        # token -> how many times a watcher for it has been cancelled for never
        # having connected. Cleared when a watcher for that token connects and
        # when the room leaves the listing, so it reads as "stuck now" rather
        # than as a lifetime tally.
        self._never_connected: dict[str, int] = {}
        self._warned_no_chat_relay = False

    # -- small helpers -----------------------------------------------------

    def count(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory(self.config)
        from ...async_runtime import get_talk_client
        return get_talk_client(self.config)

    def connect(self, url: str):
        """The WebSocket connection, as an async context manager.

        **`ping_interval` and `ping_timeout` are left at their defaults on
        purpose.** There is no client→server application ping in this protocol;
        liveness runs the other way, with the server sending a WebSocket
        *control* ping every 54 seconds and dropping a client that does not
        answer within 60 (`client/client.go:52-55`). `websockets` replies to
        control pings automatically. Passing `ping_interval=None` would only
        disable our own pings and is harmless; disabling the pong reply is not,
        and would drop every connection at 60 seconds with nothing in the
        protocol saying why.

        `max_size` is the library default rather than `None`: a relayed comment
        larger than it closes the connection, and the reconnect that follows
        runs a catch-up fetch from OCS, which is where oversized messages are
        read correctly anyway. Unbounded would be a memory budget set by the
        server.
        """
        if self._connect is not None:
            return self._connect(url)
        websockets = sig.require_websockets()
        return websockets.connect(url, open_timeout=_HANDSHAKE_TIMEOUT_SECONDS)

    def signaling_url(self, settings) -> str:
        """The operator's override, or what Talk advertises.

        An explicit `[talk.signaling] url` exists for a deployment where the
        daemon must reach the HPB by a different route than the one Nextcloud
        advertises to browsers.
        """
        return (self.config.talk.signaling.url or "").strip() or settings.server

    def note_features(self, features) -> None:
        if sig.CHAT_RELAY_FEATURE in features:
            return
        if not self._warned_no_chat_relay:
            self._warned_no_chat_relay = True
            logger.warning(
                "The signaling server does not advertise %s, so every event "
                "arrives as a bare refresh and every message costs a fetch. "
                "Inbound still works; upgrade the server to 2.1.0 or later.",
                sig.CHAT_RELAY_FEATURE,
            )
        self.count("no_chat_relay_connections")

    async def settings(self, *, discard=None):
        """One settings payload, shared across a burst of reconnects.

        Re-fetched before every non-resume hello rather than cached with a long
        TTL: Talk mints the token `exp = iat + 60` and the server allows a
        minute of leeway either side, so one held across a backoff of more than
        a minute or two comes back `token_expired`. What the short cache buys
        is the hourly ingress drop, where N watchers reconnect at once and the
        token is per user rather than per room.

        `discard` is the settings object a caller has just been refused with.
        Passing it is what makes a refresh correct under contention: another
        watcher may have re-fetched while this one waited for the lock, and
        comparing identity is what tells "mine is stale" from "someone already
        replaced it".

        **A failure is shared too, and that half was missing** (ISSUE-416). The
        lock was held across the fetch while `_settings_at` was stamped only on
        success, so the docstring's claim that N watchers make one call between
        them held only on the happy path — the path where it matters least. On
        the failure path each waiter released into an unstamped cache and made
        its own call, turning one throttled endpoint into N serial 15s waits on
        it. `_settings_error` is a short negative window over exactly the same
        lock, so the shape of a failed burst matches the shape of a good one.
        """
        now = time.monotonic()
        cached = self._settings
        if (
            discard is None
            and cached is not None
            and now - self._settings_at < _SETTINGS_TTL_SECONDS
        ):
            return cached

        async with self._settings_lock:
            current = self._settings
            if current is not None and current is not discard:
                if (
                    discard is not None
                    or time.monotonic() - self._settings_at < _SETTINGS_TTL_SECONDS
                ):
                    return current

            # Checked inside the lock rather than beside the positive fast path
            # above, because the waiters this exists for are the ones already
            # blocked on it: by the time they are woken the fetch they would
            # otherwise repeat has already happened and failed.
            self._raise_if_recently_failed()

            try:
                payload = await self._client().get_signaling_settings()
                settings = sig.parse_settings(
                    payload, nextcloud_url=self.config.nextcloud.url,
                )
                reason = sig.hpb_unavailable_reason(settings)
                if reason is not None:
                    # Not a `SignalingUnavailable`: that is the startup
                    # refusal's exception and refuses to boot. Here the
                    # deployment booted and Talk has changed its mind, which is
                    # a watcher-level fault.
                    raise RuntimeError(
                        f"Talk signaling settings unusable: {reason}"
                    )
            except asyncio.CancelledError:
                # Never cached. A cancelled fetch says nothing about the
                # server, and holding it as a shared failure would refuse
                # every other watcher over a shutdown that has nothing to do
                # with them.
                raise
            except BaseException as e:
                self._settings_error = e
                self._settings_error_at = time.monotonic()
                self.count("settings_failures")
                raise

            self._settings = settings
            self._settings_at = time.monotonic()
            self._settings_error = None
            self.count("settings_fetches")
            return settings

    def _raise_if_recently_failed(self) -> None:
        """Re-raise the shared settings failure, if one is still inside its window.

        A *fresh* exception carrying the original as `__cause__`, never the
        stored object itself: re-raising one instance from several coroutines
        accumulates their tracebacks onto it, and the log line a watcher prints
        is `type(e).__name__: e`, which would then name whichever coroutine got
        there first. Nothing branches on the type of a settings failure — it
        reaches `run`'s generic arm — so a `RuntimeError` here costs no
        handling and says out loud that the caller is being refused rather than
        having made a request.
        """
        error = self._settings_error
        if error is None:
            return
        age = time.monotonic() - self._settings_error_at
        if age >= _SETTINGS_FAILURE_TTL_SECONDS:
            self._settings_error = None
            return
        self.count("settings_failures_shared")
        raise RuntimeError(
            f"Talk signaling settings fetch failed {age:.1f}s ago and is not "
            f"being retried yet: {type(error).__name__}: {error}"
        ) from error

    async def join_room_session(self, token: str) -> str:
        """`POST …/participants/active`, refused for a token we do not watch.

        The second half of the closed-over-`list_conversations` rule, and the
        one that actually issues a request. `_may_watch` is checked at the
        point a watcher starts; this is checked at the point the POST is made,
        because those are different moments and a room can leave the listing in
        between.

        **A 404 is fatal, and that is a throttler decision rather than a
        tidiness one** (ISSUE-414). Nextcloud answers 404 for a room deleted
        between reconciliation passes; `raise_for_status` raises, and before
        this the exception fell into `RoomWatcher.run`'s generic
        `except Exception` and reconnected on the backoff for ever. Every
        attempt is another bruteforce-throttler attempt: `RoomController::
        joinRoom` carries `#[BruteForceProtection(action: 'talkRoomToken')]`
        and `SignalingController::getSettings` declares **the same action**,
        and the throttler keys on (IP, action) — so 404s on one dead room buy
        a pre-controller sleep on the settings fetch for *every* room. The
        ladder climbs to `sleepDelayOrThrowOnMax`'s 25s cap and never comes
        back down, because `joinRoom` resets the delay only on a *successful*
        join, which a deleted room can never produce. Measured on a live
        stack, same credentials, same second: the unannotated `/room` listing
        200 in 0.31s, the annotated settings call 200 in 25.31s, this 404 in
        50.32s — two sleeps, since the 404's own `throttle()` fires after the
        middleware already slept. Later the server said it aloud: 429.

        Only 404. Anything else is left retryable, because a 502 from the
        ingress or a Nextcloud restart is exactly the transient case the
        backoff exists for, and parking those for the `_FATAL_RETRY_SECONDS`
        hold-off would be the churn traded for an outage. Stopping is not
        losing the room: `reconcile` owns watcher lifecycle and starts a fresh
        watcher once the token is back in the listing.
        """
        if not self._may_watch(token):
            self.count("refused_joins")
            raise WatcherFatal(
                f"refusing to join {token}: it is not in the bot's current "
                "conversation listing"
            )
        try:
            return await self._client().join_room_session(token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            self.count("departed_rooms")
            raise WatcherFatal(
                f"{token} is gone: participants/active answered 404"
            ) from e

    async def catch_up(self, token: str) -> None:
        """Read one room forward from its cursor, behind the room's in-flight flag.

        **The flag is what makes this safe against the drain**, and skipping it
        is a correctness bug rather than a tidiness one. Both this and
        `poll_one_conversation` read the cursor under the transport lock,
        *release it*, fetch, then take it again to process — so a reconnect
        landing while a drain fetch is in flight reads the same cursor, fetches
        the same messages and runs the whole filter chain over them a second
        time. `set_talk_poll_state` is MAX-guarded and `ingest_message` dedups
        on the Talk message id, so no duplicate task results; but
        `dispatch_command`, `handle_confirmation_reply` and its ack post, the
        `!model` usage reply and the channel-gate notice are none of them
        idempotent, and those are exactly the side effects the cursor's
        monotonicity rule was written to protect. The transport lock does not
        help: it serializes the two transactions, not the read-fetch-process
        sequence around them.

        The check and the add have no `await` between them, so on one loop they
        are atomic. A room already in flight is marked dirty instead, which
        hands it to the drain — the catch-up's messages are not lost, they are
        read by the fetch that follows.
        """
        context = self._context.get(token)
        if context is None:
            self.count("catch_up_without_context")
            return
        if token in self._inflight:
            self.count("catch_up_deferred")
            self.mark_dirty(token)
            return
        self._inflight.add(token)
        try:
            created = await catch_up_conversation(
                self.config, token,
                conv_type=context.conv_type,
                display_name=context.display_name,
                client=self._client(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never fail a join on catch-up
            logger.warning(
                "Talk signaling catch-up for %s failed: %s: %s",
                token, type(e).__name__, e,
            )
            self.count("catch_up_failures")
            # The room is now behind its cursor, so the next reconciliation
            # fetches it. Marking it dirty here would retry immediately.
            return
        finally:
            self._inflight.discard(token)
        if created:
            logger.info(
                "Talk signaling catch-up queued %d task(s) in %s",
                len(created), token,
            )

    # -- the coalescing queue ---------------------------------------------

    def _may_watch(self, token) -> bool:
        return isinstance(token, str) and bool(token) and token in self._live

    def mark_dirty(self, token, *, watcher_token: str | None = None) -> None:
        """Queue one room for a fetch. At most one in flight per room.

        The token is the one the *server* named in the event. It is checked
        against the live set rather than trusted, and a watcher naming a room
        other than its own is refused outright: one session holds one room, so
        that combination means either a server bug or a frame nobody should be
        acting on.
        """
        if watcher_token is not None and token != watcher_token:
            self.count("foreign_room_events")
            logger.warning(
                "Talk signaling watcher for %s reported an event for %s; "
                "ignoring", watcher_token, token,
            )
            return
        if token not in self._context or not self._may_watch(token):
            self.count("unknown_room_events")
            return
        # The *first* stamp wins, so the age of a dirty bit is how long the
        # room has been owed a fetch rather than how long since the last event.
        self._dirty.setdefault(token, time.monotonic())
        self._wake.set()

    async def _drain_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._drain_once()

    async def _drain_once(self) -> None:
        """Fetch every dirty room once, in one pass.

        A room whose fetch fails is restored to the dirty map and **skipped for
        the rest of this pass**, which is what keeps "preserve the dirty bit"
        from becoming a hot retry loop against a transaction that just raised.
        """
        attempted: set[str] = set()
        while True:
            token = next(
                (
                    t for t in self._dirty
                    if t not in self._inflight and t not in attempted
                ),
                None,
            )
            if token is None:
                return
            attempted.add(token)
            marked_at = self._dirty.pop(token)
            context = self._context.get(token)
            if context is None:
                # A drain that cannot find context does not guess: the results
                # block would read `conv_types.get(token, 1)`, which is a DM,
                # and skip the @mention gate for the whole room.
                self.count("drain_without_context")
                continue

            self._inflight.add(token)
            try:
                created = await poll_one_conversation(
                    self.config, token,
                    conv_type=context.conv_type,
                    display_name=context.display_name,
                    client=self._client(),
                )
                self.count("fetches")
                if created:
                    logger.info(
                        "Talk signaling queued %d task(s) in %s",
                        len(created), token,
                    )
            except asyncio.CancelledError:
                # Cancellation is not a failed fetch, but the room is still
                # owed one and the supervisor is going away, so the bit is put
                # back for whatever runs next.
                self._dirty.setdefault(token, marked_at)
                raise
            except Exception as e:  # noqa: BLE001 — never raises into the loop
                self.count("fetch_failures")
                logger.error(
                    "Talk signaling fetch for %s failed: %s: %s",
                    token, type(e).__name__, e,
                )
                # Preserved, not re-woken. See the module docstring.
                self._dirty.setdefault(token, marked_at)
            finally:
                # In a `finally` rather than on the success path: clearing it
                # only when the fetch worked strands the room for the life of
                # the process, and nothing notices because the socket is fine.
                self._inflight.discard(token)

    # -- reconciliation ----------------------------------------------------

    async def reconcile(self) -> None:
        """One room pass: register, decide the watcher set, fetch what is behind."""
        if not self.config.talk.enabled or not self.config.nextcloud.url:
            # `reconcile_talk_rooms` answers this with the same empty pass it
            # returns for a failed listing, and counting a configuration state
            # as an outage makes the two indistinguishable in `stats()` for the
            # life of the process.
            self.count("reconcile_unconfigured")
            return
        try:
            room_pass = await reconcile_talk_rooms(
                self.config, client=self._client(),
                # Explicit, so this loop neither reads nor stamps the poller's
                # `_last_full_sweep` clock: the two drivers never both run, but
                # a supervisor that moved that clock would change what a later
                # poll-only deployment does on its first cycle after a restart.
                full_sweep=False,
                # The poller archives on its sweep; this loop's own interval
                # *is* that cadence, so it asks directly. The non-empty
                # token-set guard is unchanged.
                archive=True,
                # Without this the room pass builds a live
                # `_poll_single_conversation` coroutine per room behind its
                # cursor and nobody awaits one: N held 30-second requests
                # issued and abandoned, plus a "never awaited" warning. Here
                # the fetch is the drain's job, one room at a time behind the
                # transport lock, so the pass hands back the decision instead.
                open_poll=lambda _client, token, cursor, _timeout: (token, cursor),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Talk room reconciliation failed: %s: %s", type(e).__name__, e,
            )
            self.count("reconcile_failures")
            return

        if room_pass.listing_failed:
            self.count("reconcile_failures")
            return

        self._live = set(room_pass.live_tokens)
        self._context = dict(room_pass.context)
        self._watchable = set(room_pass.watchable)
        # `behind` is `(token, cursor)` pairs here rather than coroutines,
        # because `open_poll` is left at its default only on the poll path.
        behind = [
            entry[0] if isinstance(entry, tuple) else entry
            for entry in room_pass.behind
        ]
        # The **diagnostic** is the strict set, not the work list. The gate
        # un-gates a room both for being ahead of its cursor and for having no
        # cursor to compare against, and the second state is permanent for an
        # empty room — so counting it here would have `doctor` report the event
        # stream as failing for ever on a deployment where it works. Both are
        # still fetched.
        self._rooms_behind = len(room_pass.behind_cursor)

        await self._stop_departed()
        await self._restart_never_connected()
        for token in behind:
            self.mark_dirty(token)
        self._start_missing()

    async def _stop_departed(self) -> None:
        gone = [t for t in self._tasks if t not in self._watchable]
        for token in gone:
            await self._stop_watcher(token)
            # A room we no longer watch must not keep a queue entry, or the
            # drain would fetch a room the listing no longer names.
            self._dirty.pop(token, None)
        # A room that left the listing altogether takes its hold-off with it,
        # so re-invitation is a clean start rather than an hour of silence.
        for token in list(self._fatal_until):
            if token not in self._watchable:
                self._fatal_until.pop(token, None)
        for token in list(self._never_connected):
            if token not in self._watchable:
                self._never_connected.pop(token, None)

    async def _restart_never_connected(self) -> None:
        """Cancel a watcher that is alive and has never once connected.

        **The escalation `_start_missing` structurally cannot make** (ISSUE-416).
        That loop skips any token whose task is not `done()`, which is right for
        a watcher between reconnects and wrong for one that has never reached a
        socket — and before `ever_connected` there was nothing to tell those
        apart. A wedged watcher therefore looked exactly like a flapping one,
        for ever: the supervisor reported it present, the reconciler was
        healthy, `doctor` saw a socket count that added up, and one room was
        simply dark. That is why ISSUE-414 was invisible from outside.

        A restart is a real repair for one shape and only a report for the
        other, and both are worth having. A watcher stuck on an await that never
        returns cannot recover on its own — its backoff loop is *inside* the
        task — so cancelling is the only thing that reaches it. A watcher whose
        every attempt fails fast is not repaired by a fresh task, but the
        cancellation is what puts the token in `never_connected` and in front of
        an operator.

        Bounded by the reconciliation cadence, which is the same argument the
        fatal hold-off makes: one settings fetch, one `participants/active` POST
        and one connect per stuck room per pass, never a loop of its own.
        """
        for token, watcher in list(self._watchers.items()):
            if watcher.ever_connected:
                # Proof the whole chain works for this room. Anything after
                # this is the reconnect loop, which is not this method's
                # business.
                self._never_connected.pop(token, None)
                continue
            task = self._tasks.get(token)
            if task is None or task.done():
                # `_start_missing` owns an ended task, including deciding
                # whether it ended fatally. Reaching in here would race it.
                continue
            if time.monotonic() - watcher.started_at < _NEVER_CONNECTED_SECONDS:
                continue
            logger.warning(
                "Talk signaling watcher for %s has never connected in %.0fs; "
                "restarting it", token, _NEVER_CONNECTED_SECONDS,
            )
            await self._stop_watcher(token)
            # After `_stop_watcher`, which pops the token out of every other
            # mapping it touches.
            self._never_connected[token] = self._never_connected.get(token, 0) + 1
            self.count("watchers_never_connected")

    def _start_missing(self) -> None:
        now = time.monotonic()
        for token in sorted(self._watchable):
            task = self._tasks.get(token)
            if task is not None and not task.done():
                continue
            if task is not None:
                # Ended for good — a fatal code, or an exception that escaped.
                # Restarted here rather than from a done-callback: this loop
                # runs on `room_sync_interval`, which is the bounded, capped
                # cadence the restart needs, and the handle is known finished.
                watcher = self._watchers.pop(token, None)
                self._tasks.pop(token, None)
                if watcher is not None and watcher.fatal:
                    # `WatcherFatal` means this will not fix itself, so
                    # restarting it every `room_sync_interval` is the churn the
                    # exception exists to avoid: `doctor` would report watchers
                    # that are trying rather than one that cannot work, and each
                    # attempt costs a settings fetch, a `participants/active`
                    # POST and a connect. Held off rather than abandoned, so an
                    # operator who fixes the deployment needs no restart.
                    self._fatal_until[token] = now + _FATAL_RETRY_SECONDS
                    self.count("watchers_fatal")
                else:
                    self.count("watchers_restarted")
            held = self._fatal_until.get(token)
            if held is not None:
                if now < held:
                    continue
                self._fatal_until.pop(token, None)
                self.count("watchers_retried_after_fatal")
            if not self._may_watch(token):
                self.count("refused_joins")
                continue
            watcher = RoomWatcher(self, token)
            self._watchers[token] = watcher
            self._tasks[token] = asyncio.create_task(
                watcher.run(), name=f"talk-signaling-{token}",
            )

    async def _stop_watcher(self, token: str) -> None:
        task = self._tasks.pop(token, None)
        self._watchers.pop(token, None)
        self._fatal_until.pop(token, None)
        if task is None:
            return
        task.cancel()
        # Awaited, not merely cancelled. This is what a `concurrent.futures`
        # handle cannot give: the watcher's `finally` has run by the time this
        # returns, so nothing starts a second watcher on top of a socket and a
        # Talk room session the first one still holds.
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        sig.set_stats_source(self.stats)
        drain = self._spawn_drain()
        try:
            while not self._stop.is_set():
                # **The whole pass, not just the listing call.** `reconcile`
                # guards its own network step, but `_stop_departed` awaits a
                # cancelled watcher task and re-raises anything that was not a
                # `CancelledError` — and an exception escaping here unwinds
                # straight into the `finally` below, tearing down the drain and
                # every watcher and leaving Talk inbound dead for the life of
                # the process with nothing saying why. One bad pass costs a
                # cycle instead.
                try:
                    await self.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — one pass, not the daemon
                    self.count("reconcile_failures")
                    logger.error(
                        "Talk signaling reconciliation pass failed: %s: %s",
                        type(e).__name__, e,
                    )

                # A drain that died takes the whole trigger path with it, and
                # invisibly: every counter `doctor` reads stays healthy while
                # `mark_dirty` sets an event nobody is waiting on. That is the
                # module's own stranding failure one level up, so it is checked
                # here rather than left to a done-callback whose exception
                # nobody retrieves.
                if drain.done():
                    self.count("drain_restarted")
                    logger.error(
                        "Talk signaling drain task ended (%r); restarting it",
                        drain.exception() if not drain.cancelled() else "cancelled",
                    )
                    drain = self._spawn_drain()
                    # Anything queued while it was dead is still queued.
                    if self._dirty:
                        self._wake.set()

                interval = max(
                    _MIN_SYNC_INTERVAL_SECONDS,
                    float(self.config.talk.signaling.room_sync_interval or 0),
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
        finally:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
            # Cancel every watcher first, then wait for them together.
            # `AsyncRuntime.stop` gives the whole shutdown half its budget
            # (5 seconds by default), and each cancel unwinds through a
            # WebSocket close handshake — so N sequential awaits spend N close
            # handshakes out of one budget where they can all run at once.
            tasks = [self._tasks.pop(t) for t in list(self._tasks)]
            self._watchers.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            sig.clear_stats_source()

    def _spawn_drain(self) -> asyncio.Task:
        return asyncio.create_task(
            self._drain_loop(), name="talk-signaling-drain",
        )

    def stop(self) -> None:
        """Ask `run` to return. Cancellation is the other way in and is fine."""
        self._stop.set()

    def stats(self) -> dict:
        """The counters `doctor`'s `talk.signaling_watchers` reads.

        Four keys are the contract (`signaling.read_stats`): `watchers`,
        `connected`, `disconnected` and `rooms_behind`. The rest is diagnosis
        and is ignored by that check.

        `stale_dirty` is the one the drain's error contract makes necessary: a
        room whose dirty bit has outlived a `room_sync_interval` is one whose
        fetch failed and has not been retried, and it is invisible from every
        other angle because the socket is fine.
        """
        now = time.monotonic()
        interval = max(
            _MIN_SYNC_INTERVAL_SECONDS,
            float(self.config.talk.signaling.room_sync_interval or 0),
        )
        # **Snapshotted before anything walks them.** This is called from
        # whichever thread `doctor` runs on while the loop thread mutates all
        # four mappings, and a comprehension over a live dict raises
        # `RuntimeError: dictionary changed size during iteration`.
        # `read_stats` catches that and returns `None`, so the visible symptom
        # would be `doctor` intermittently reporting "no signaling supervisor
        # is running in this process" on a healthy deployment — the exact
        # misreport that registration exists to avoid. `list(d)` is a single
        # C-level call under the GIL and cannot be interrupted part-way.
        watchers = list(self._watchers.items())
        marks = list(self._dirty.values())
        stuck = list(self._never_connected)
        # The counters go in first, so a diagnostic that happens to be named
        # like one of the four contract keys can never displace it.
        stats = dict(self._counters)
        stats.update({
            "watchers": len(self._tasks),
            "connected": sum(1 for _t, w in watchers if w.connected),
            "disconnected": sorted(
                token for token, w in watchers if not w.connected
            ),
            "rooms_behind": self._rooms_behind,
            # Distinct from `disconnected`, which a watcher joins for a second
            # between reconnects. These are rooms nothing has ever delivered
            # for, which reads the same as a healthy one on every other number
            # here.
            "never_connected": sorted(stuck),
            "dirty": len(marks),
            "in_flight": len(self._inflight),
            "stale_dirty": sum(1 for marked in marks if now - marked > interval),
        })
        return stats
