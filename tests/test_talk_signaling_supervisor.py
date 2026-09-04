"""The supervisor: the watcher set, the queue, and the shape `doctor` reads.

Three properties this file exists for, none of which the protocol tests can
see because none of them is about a frame:

- **A watcher may only join a token the current listing returned.** Nextcloud
  does *not* hand this property over: `ParticipantService::joinRoom` self-enrols
  the caller in a listable group room and in any public one, so
  `participants/active` on an arbitrary token is not guaranteed to fail — it can
  quietly make istota a participant. The only source of tokens is the bot's own
  conversation listing, and that is ours to maintain.
- **The coalescing rule, with its error contract.** N events during one
  in-flight fetch cost one more fetch, and a *failed* fetch preserves the dirty
  bit. Clearing in-flight only on the success path strands the room for the life
  of the process and nothing notices, because the socket is fine.
- **`stats()` produces what `doctor` reads.** Every earlier test of
  `talk.signaling_watchers` drove it through a registered stats source rather
  than a real supervisor, so this is the first place the two meet.
"""

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock

from istota import db, doctor
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    TalkSignalingConfig,
    UserConfig,
)
from istota.transport.talk import inbound as poller
from istota.transport.talk import signaling as sig
from istota.transport.talk.supervisor import SignalingSupervisor


@pytest.fixture(autouse=True)
def _reset_module_state():
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None
    sig.clear_stats_source()
    yield
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None
    sig.clear_stats_source()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    cfg = Config()
    cfg.db_path = path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.skills_dir = tmp_path / "skills"
    cfg.skills_dir.mkdir(exist_ok=True)
    cfg.talk = TalkConfig(
        enabled=True, bot_username="istota",
        signaling=TalkSignalingConfig(enabled=True, room_sync_interval=300),
    )
    cfg.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    cfg.users = {"alice": UserConfig()}
    cfg.scheduler = SchedulerConfig()
    return cfg


def _client(conversations=None):
    client = AsyncMock()
    client.list_conversations = AsyncMock(return_value=conversations or [])
    client.get_participants = AsyncMock(return_value=[])
    client.fetch_chat_history = AsyncMock(return_value=[])
    client.get_latest_message_id = AsyncMock(return_value=500)
    client.poll_messages = AsyncMock(return_value=[])
    client.fetch_messages_since = AsyncMock(return_value=[])
    client.join_room_session = AsyncMock(return_value="talk-session-1")
    client.get_signaling_settings = AsyncMock(return_value={
        "server": "https://hpb.test/standalone-signaling",
        "signalingMode": "external",
        "helloAuthParams": {"2.0": {"token": "jwt"}},
        "userId": "istota",
    })
    return client


def _supervisor(config, client):
    return SignalingSupervisor(config, client_factory=lambda _c: client)


class TestTheWatcherSetIsClosedOverTheListing:
    """A token the listing did not return is never joined."""

    @pytest.mark.asyncio
    async def test_a_token_outside_the_live_set_is_refused(self, config):
        client = _client([
            {"token": "mine", "type": 2, "displayName": "team",
             "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "mine", 10)

        await sup.reconcile()
        assert sup.stats()["watchers"] == 1

        # A public or listable room the bot is not in. Nextcloud would accept
        # the POST and enrol it; this is the guard that never issues one.
        with pytest.raises(Exception):
            await sup.join_room_session("someone-elses-room")

        assert client.join_room_session.await_count == 0
        assert sup.stats()["refused_joins"] == 1

        await sup._stop_watcher("mine")

    @pytest.mark.asyncio
    async def test_a_watched_token_is_joined(self, config):
        """The control for the test above: the guard passes a live token."""
        client = _client([
            {"token": "mine", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "mine", 10)

        await sup.reconcile()
        assert await sup.join_room_session("mine") == "talk-session-1"
        assert client.join_room_session.await_args.args == ("mine",)

        await sup._stop_watcher("mine")

    @pytest.mark.asyncio
    async def test_an_event_naming_another_room_is_dropped(self, config):
        """A watcher holds one room; a frame naming another is not acted on."""
        client = _client([
            {"token": "a", "type": 2, "lastMessage": {"id": 10}},
            {"token": "b", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "a", 10)
            db.set_talk_poll_state(conn, "b", 10)

        await sup.reconcile()
        sup._dirty.clear()

        sup.mark_dirty("b", watcher_token="a")
        assert sup._dirty == {}
        assert sup.stats()["foreign_room_events"] == 1

        sup.mark_dirty("nowhere", watcher_token="nowhere")
        assert sup._dirty == {}
        assert sup.stats()["unknown_room_events"] == 1

        for token in ("a", "b"):
            await sup._stop_watcher(token)


class TestACursorlessRoomGetsNoWatcher:
    """Catch-up reads forward from the cursor; a NULL one reads from zero."""

    @pytest.mark.asyncio
    async def test_a_room_whose_cursor_init_failed_is_not_watched(self, config):
        client = _client([
            {"token": "grp", "type": 2, "lastMessage": {"id": 10}},
        ])
        client.get_latest_message_id = AsyncMock(
            side_effect=RuntimeError("nextcloud is down"),
        )
        sup = _supervisor(config, client)

        await sup.reconcile()

        assert sup.stats()["watchers"] == 0
        assert client.join_room_session.await_count == 0


class TestTheCoalescingQueue:
    """One in-flight fetch per room, and a `finally` that always clears it."""

    def _armed(self, monkeypatch, *, fail=False):
        """Install a fetch that blocks until the test lets it finish."""
        import istota.transport.talk.supervisor as mod

        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fetch(_config, token, *, conv_type, display_name, client=None):
            calls.append(token)
            started.set()
            await release.wait()
            if fail:
                raise RuntimeError("the results transaction rolled back")
            return []

        monkeypatch.setattr(mod, "poll_one_conversation", fetch)
        return started, release, calls

    @pytest.mark.asyncio
    async def test_n_events_during_a_fetch_cost_exactly_one_more(
        self, config, monkeypatch,
    ):
        client = _client([
            {"token": "grp", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 10)
        await sup.reconcile()
        sup._dirty.clear()

        started, release, calls = self._armed(monkeypatch)
        try:
            sup.mark_dirty("grp")
            drain = asyncio.create_task(sup._drain_loop())
            await asyncio.wait_for(started.wait(), timeout=5)

            # Ten more events while the first fetch is still running.
            for _ in range(10):
                sup.mark_dirty("grp")
            assert sup._inflight == {"grp"}

            release.set()
            # Let the first fetch finish and the pass pick up the dirty bit.
            for _ in range(50):
                await asyncio.sleep(0)
                if len(calls) > 1:
                    break

            assert calls == ["grp", "grp"], calls
            assert sup._dirty == {}
            assert sup._inflight == set()
        finally:
            drain.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain
            await sup._stop_watcher("grp")

    @pytest.mark.asyncio
    async def test_a_failed_fetch_preserves_the_dirty_bit(
        self, config, monkeypatch,
    ):
        client = _client([
            {"token": "grp", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 10)
        await sup.reconcile()
        sup._dirty.clear()

        started, release, calls = self._armed(monkeypatch, fail=True)
        try:
            sup.mark_dirty("grp")
            marked_at = sup._dirty["grp"]
            release.set()
            await sup._drain_once()

            assert calls == ["grp"]
            # In-flight cleared in the `finally` — the room is not stranded.
            assert sup._inflight == set()
            # Dirty bit preserved, with its original stamp, so the age is how
            # long the room has been owed a fetch.
            assert sup._dirty == {"grp": marked_at}
            assert sup.stats()["fetch_failures"] == 1

            # And not retried inside the same pass: an immediate re-run of a
            # transaction that just raised is a hot loop against Nextcloud.
            await sup._drain_once()
            assert calls == ["grp", "grp"]
        finally:
            await sup._stop_watcher("grp")

    @pytest.mark.asyncio
    async def test_a_stale_dirty_bit_is_counted_for_doctor(self, config):
        client = _client([
            {"token": "grp", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 10)
        await sup.reconcile()

        sup._dirty["grp"] = time.monotonic() - 10_000
        assert sup.stats()["stale_dirty"] == 1

        await sup._stop_watcher("grp")


class TestTheStatsShapeIsTheOneDoctorReads:
    """The first place a real supervisor meets the check that reads it."""

    @pytest.mark.asyncio
    async def test_doctor_reads_a_live_supervisor(self, config):
        client = _client([
            {"token": "a", "type": 2, "lastMessage": {"id": 10}},
            {"token": "b", "type": 2, "lastMessage": {"id": 10}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "a", 10)
            db.set_talk_poll_state(conn, "b", 10)
        await sup.reconcile()

        sig.set_stats_source(sup.stats)
        try:
            stats = sig.read_stats()
            assert stats["watchers"] == 2
            assert stats["connected"] == 0
            assert sorted(stats["disconnected"]) == ["a", "b"]
            assert stats["rooms_behind"] == 0

            result = doctor.check_signaling_watchers(config, probe=False)
            assert result.name == "talk.signaling_watchers"
            # Two watchers, neither connected yet — the check has to say so
            # rather than skipping or crashing on a shape it did not expect.
            assert result.status == doctor.WARN
            assert "0 of 2 watchers connected" in result.detail

            # Connected, and the same check goes green off the same object.
            for watcher in sup._watchers.values():
                watcher.connected = True
            assert doctor.check_signaling_watchers(
                config, probe=False,
            ).status == doctor.OK
        finally:
            sig.clear_stats_source()
            for token in ("a", "b"):
                await sup._stop_watcher(token)

    @pytest.mark.asyncio
    async def test_rooms_behind_is_the_listing_comparison(self, config):
        """The one number that distinguishes a stream from its safety net."""
        client = _client([
            {"token": "grp", "type": 2, "lastMessage": {"id": 900}},
        ])
        sup = _supervisor(config, client)
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 10)

        await sup.reconcile()

        assert sup.stats()["rooms_behind"] == 1
        # And the room was queued rather than fetched inside the pass: the
        # reconciler applies the writes and hands the fetch to the drain.
        assert "grp" in sup._dirty
        assert client.poll_messages.await_count == 0

        await sup._stop_watcher("grp")


class TestTheSettingsFetchIsSharedAcrossABurst:
    @pytest.mark.asyncio
    async def test_n_watchers_reconnecting_make_one_settings_call(self, config):
        client = _client()
        sup = _supervisor(config, client)

        results = await asyncio.gather(*[sup.settings() for _ in range(20)])

        assert client.get_signaling_settings.await_count == 1
        assert all(s is results[0] for s in results)
        assert results[0].server == "https://hpb.test/standalone-signaling"

    @pytest.mark.asyncio
    async def test_a_refused_token_forces_exactly_one_refetch(self, config):
        client = _client()
        sup = _supervisor(config, client)

        first = await sup.settings()
        # Twenty watchers refused with the same stale object refetch once
        # between them, which is the hourly-ingress-drop shape.
        again = await asyncio.gather(
            *[sup.settings(discard=first) for _ in range(20)]
        )

        assert client.get_signaling_settings.await_count == 2
        assert all(s is again[0] for s in again)
        assert again[0] is not first

    @pytest.mark.asyncio
    async def test_a_deployment_with_no_hpb_is_a_watcher_fault(self, config):
        client = _client()
        client.get_signaling_settings = AsyncMock(return_value={
            "server": "", "signalingMode": "internal", "helloAuthParams": {},
        })
        sup = _supervisor(config, client)

        with pytest.raises(RuntimeError, match="internal signaling mode"):
            await sup.settings()


class _Closed(Exception):
    """What a fake socket raises when its script runs out."""


class _FakeWS:
    """A scripted WebSocket. Records what was sent; hands back what was set."""

    def __init__(self, frames, *, hang_after=False):
        self._frames = list(frames)
        self._hang_after = hang_after
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self._frames:
            return json.dumps(self._frames.pop(0))
        if self._hang_after:
            await asyncio.Event().wait()
        raise _Closed("the server went away")


_WELCOME = {"type": "welcome", "welcome": {
    "version": "2.1.1", "features": ["hello-v2", "chat-relay"],
}}
_HELLO_OK = {"type": "hello", "hello": {
    "sessionid": "public-session", "resumeid": "RESUME-SECRET",
}}
_ROOM_OK = {"type": "room", "room": {"roomid": "grp"}}


def _event(token="grp", comment=True):
    chat = {"refresh": True}
    if comment:
        chat = {"refresh": True, "comment": {"id": 7, "message": "hi"}}
    return {"type": "event", "event": {
        "target": "room", "type": "message",
        "message": {"roomid": token, "data": {"type": "chat", "chat": chat}},
    }}


async def _armed_supervisor(config, client, frames, *, hang_after=False):
    """A supervisor with one live room and a scripted socket for it."""
    ws = _FakeWS(frames, hang_after=hang_after)
    sup = SignalingSupervisor(
        config, client_factory=lambda _c: client, connect=lambda _url: ws,
    )
    sup._live = {"grp"}
    sup._watchable = {"grp"}
    sup._context = {"grp": poller.RoomContext(conv_type=2, display_name="team")}
    return sup, ws


class TestTheConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_one_session_hellos_joins_catches_up_and_consumes(
        self, config,
    ):
        from istota.transport.talk.supervisor import RoomWatcher

        client = _client()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 41)
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, _HELLO_OK, _ROOM_OK, _event()],
        )
        watcher = RoomWatcher(sup, "grp")

        with pytest.raises(_Closed):
            await watcher._session()

        hello, join = ws.sent
        assert hello["type"] == "hello"
        assert hello["hello"]["version"] == "2.0"
        assert hello["hello"]["features"] == ["chat-relay"]
        assert "internal-incall" not in hello["hello"]["features"]
        assert join["room"] == {"roomid": "grp", "sessionid": "talk-session-1"}

        # Catch-up is `fetch_messages_since`, from the cursor. `poll_messages`
        # would hold a Nextcloud worker for 30s on every reconnect, and the
        # ingress drops every connection hourly.
        assert client.fetch_messages_since.await_count == 1
        assert client.fetch_messages_since.await_args.kwargs["since_id"] == 41
        assert client.poll_messages.await_count == 0

        # The event triggered a fetch rather than being consumed as a payload.
        assert "grp" in sup._dirty
        assert sup.stats()["comment_events"] == 1

    @pytest.mark.asyncio
    async def test_no_such_room_is_retried_once_with_a_fresh_session(
        self, config,
    ):
        from istota.transport.talk.supervisor import RoomWatcher

        client = _client()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 41)
        refusal = {"type": "error", "error": {
            "code": "no_such_room", "message": "No such room",
        }}
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, _HELLO_OK, refusal, _ROOM_OK],
        )
        watcher = RoomWatcher(sup, "grp")

        with pytest.raises(_Closed):
            await watcher._session()

        # A stale Talk session is the likely cause after an outage, and the fix
        # is a fresh `participants/active` rather than a fresh token.
        assert client.join_room_session.await_count == 2

    @pytest.mark.asyncio
    async def test_a_second_no_such_room_stops_the_watcher(self, config):
        from istota.transport.talk.supervisor import RoomWatcher, WatcherFatal

        client = _client()
        refusal = {"type": "error", "error": {"code": "no_such_room"}}
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, _HELLO_OK, refusal, refusal],
        )
        watcher = RoomWatcher(sup, "grp")

        with pytest.raises(WatcherFatal):
            await watcher._session()

        # Guarded against a loop by the recovery budget, not by a comment. The
        # room is left to the next reconciliation to decide.
        assert client.join_room_session.await_count == 2

    @pytest.mark.asyncio
    async def test_invalid_backend_is_fatal_rather_than_retried(self, config):
        from istota.transport.talk.supervisor import RoomWatcher, WatcherFatal

        client = _client()
        refusal = {"type": "error", "error": {
            "code": "invalid_backend", "message": "Unsupported backend",
        }}
        sup, ws = await _armed_supervisor(config, client, [_WELCOME, refusal])
        watcher = RoomWatcher(sup, "grp")

        with pytest.raises(WatcherFatal):
            await watcher._session()
        assert client.join_room_session.await_count == 0

    @pytest.mark.asyncio
    async def test_a_refused_resume_falls_back_to_a_full_hello(self, config):
        from istota.transport.talk.supervisor import RoomWatcher

        client = _client()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 41)
        gone = {"type": "error", "error": {"code": "no_such_session"}}
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, gone, _HELLO_OK, _ROOM_OK],
        )
        watcher = RoomWatcher(sup, "grp")
        watcher._state.resume_id = "STALE-RESUME"

        with pytest.raises(_Closed):
            await watcher._session()

        resume, hello, _join = ws.sent
        assert resume["hello"] == {"version": "1.0", "resumeid": "STALE-RESUME"}
        assert hello["hello"]["version"] == "2.0"

    @pytest.mark.asyncio
    async def test_cancellation_is_re_raised_rather_than_backed_off(
        self, config,
    ):
        """A watcher that folded cancellation into its own retry loop would
        outlive `AsyncRuntime.stop`'s budget, after which the cleanup hooks
        close the shared TalkClient under a request it is still awaiting."""
        from istota.transport.talk.supervisor import RoomWatcher

        client = _client()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 41)
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, _HELLO_OK, _ROOM_OK], hang_after=True,
        )
        watcher = RoomWatcher(sup, "grp")

        task = asyncio.create_task(watcher.run())
        for _ in range(100):
            await asyncio.sleep(0)
            if watcher.connected:
                break
        assert watcher.connected

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_no_per_connection_credential_reaches_a_log_record(
        self, config, caplog,
    ):
        """The JWT, the v1 ticket and the resumeid, at DEBUG, over a real run.

        The `resumeid` is the one most easily overlooked: it authenticates a
        full session resume, room membership included, for its 30-second
        window.
        """
        from istota.transport.talk.supervisor import RoomWatcher

        client = _client()
        client.get_signaling_settings = AsyncMock(return_value={
            "server": "https://hpb.test/standalone-signaling",
            "signalingMode": "external",
            "helloAuthParams": {
                "1.0": {"userid": "istota", "ticket": "V1-TICKET-FIXTURE"},
                "2.0": {"token": "JWT-FIXTURE"},
            },
        })
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "grp", 41)
        sup, ws = await _armed_supervisor(
            config, client, [_WELCOME, _HELLO_OK, _ROOM_OK, _event()],
        )
        watcher = RoomWatcher(sup, "grp")

        with caplog.at_level("DEBUG"):
            with pytest.raises(_Closed):
                await watcher._session()

        blob = "\n".join(
            r.getMessage() for r in caplog.records
        ) + "\n".join(str(r.args) for r in caplog.records)
        for secret in ("JWT-FIXTURE", "V1-TICKET-FIXTURE", "RESUME-SECRET"):
            assert secret not in blob, secret
        # The public session id is not a credential and is what makes a
        # connection traceable at all, so it is deliberately present.
        assert "public-session" in blob
