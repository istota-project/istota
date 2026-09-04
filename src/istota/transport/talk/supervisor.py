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

# A handshake frame that never arrives must not hold a watcher for ever. The
# server's own deadline runs the other way (2s to send `hello` after
# connecting), so this bounds our side of the same exchange.
_HANDSHAKE_TIMEOUT_SECONDS = 15.0

# The reconcile loop wakes at least this often even with a nonsense interval
# configured, so a supervisor cannot become a busy loop on a bad value.
_MIN_SYNC_INTERVAL_SECONDS = 5.0


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
        self._state = _RoomWatcherState()
        self._frame_seq = 0

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
            try:
                await self._session()
                # A clean end of the socket is not evidence of a broken
                # deployment, so the ladder starts again. The hourly ingress
                # drop is this case, N times a day.
                attempt = 0
            except asyncio.CancelledError:
                raise
            except WatcherFatal as e:
                logger.error(
                    "Talk signaling watcher for %s stopped: %s", self.token, e,
                )
                self._sup.count("watchers_stopped")
                return
            except Exception as e:  # noqa: BLE001 — a watcher never raises out
                logger.warning(
                    "Talk signaling watcher for %s disconnected: %s: %s",
                    self.token, type(e).__name__, e,
                )
            finally:
                self.connected = False

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
        self._counters: dict[str, int] = {}
        self._settings = None
        self._settings_at = 0.0
        self._settings_lock = asyncio.Lock()
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

            payload = await self._client().get_signaling_settings()
            settings = sig.parse_settings(
                payload, nextcloud_url=self.config.nextcloud.url,
            )
            reason = sig.hpb_unavailable_reason(settings)
            if reason is not None:
                # Not a `SignalingUnavailable`: that is the startup refusal's
                # exception and refuses to boot. Here the deployment booted and
                # Talk has changed its mind, which is a watcher-level fault.
                raise RuntimeError(f"Talk signaling settings unusable: {reason}")
            self._settings = settings
            self._settings_at = time.monotonic()
            self.count("settings_fetches")
            return settings

    async def join_room_session(self, token: str) -> str:
        """`POST …/participants/active`, refused for a token we do not watch.

        The second half of the closed-over-`list_conversations` rule, and the
        one that actually issues a request. `_may_watch` is checked at the
        point a watcher starts; this is checked at the point the POST is made,
        because those are different moments and a room can leave the listing in
        between.
        """
        if not self._may_watch(token):
            self.count("refused_joins")
            raise WatcherFatal(
                f"refusing to join {token}: it is not in the bot's current "
                "conversation listing"
            )
        return await self._client().join_room_session(token)

    async def catch_up(self, token: str) -> None:
        context = self._context.get(token)
        if context is None:
            self.count("catch_up_without_context")
            return
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
        self._rooms_behind = len(behind)

        await self._stop_departed()
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

    def _start_missing(self) -> None:
        for token in sorted(self._watchable):
            task = self._tasks.get(token)
            if task is not None and not task.done():
                continue
            if task is not None:
                # Ended for good — a fatal code, or an exception that escaped.
                # Restarted here rather than from a done-callback: this loop
                # runs on `room_sync_interval`, which is the bounded, capped
                # cadence the restart needs, and the handle is known finished.
                self._tasks.pop(token, None)
                self._watchers.pop(token, None)
                self.count("watchers_restarted")
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
        drain = asyncio.create_task(self._drain_loop(), name="talk-signaling-drain")
        try:
            while not self._stop.is_set():
                await self.reconcile()
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
            for token in list(self._tasks):
                await self._stop_watcher(token)
            sig.clear_stats_source()

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
        # The counters go in first, so a diagnostic that happens to be named
        # like one of the four contract keys can never displace it.
        stats = dict(self._counters)
        stats.update({
            "watchers": len(self._tasks),
            "connected": sum(
                1 for w in self._watchers.values() if w.connected
            ),
            "disconnected": sorted(
                token for token, w in self._watchers.items()
                if not w.connected
            ),
            "rooms_behind": self._rooms_behind,
            "dirty": len(self._dirty),
            "in_flight": len(self._inflight),
            "stale_dirty": sum(
                1 for marked in self._dirty.values() if now - marked > interval
            ),
        })
        return stats
