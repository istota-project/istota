"""Revocation across chat for group-owned rooms (group-owned rooms, Stage 3).

Membership synchronization (Stage 2) drops a removed member's roster row,
presence and handle in the membership transaction. This file holds the
backstop and the in-flight half: the room-access resolver asks the live group
as well as the roster, so an archived group's rooms are suspended with their
rosters intact and a row written around the group admits nobody; a hidden
group room keeps its member; leave and delete are group membership, not room
actions; a removed member's unfinished tasks are cancelled, the running ones
signalled after the commit, and anything that still completes is not
delivered; the room and task streams stop on the next batch.

Everything runs on real SQLite connections, through the group service, the
chat routes, the stream generators and `process_one_task`.
"""

import json
import signal
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebChatConfig, WebConfig
from istota.groups import service
from istota.groups.rooms import create_group_room

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

_needs_web = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")
ORIGIN = {"origin": "https://example.com"}


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    cfg = Config(
        db_path=db_path,
        workspace_path=mount,
        temp_dir=temp,
        site=SiteConfig(hostname="example.com"),
        users={name: UserConfig(display_name=name.title())
               for name in ("alice", "bob", "carol")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            chat=WebChatConfig(
                room_stream_poll_interval_ms=5,
                room_stream_room_check_seconds=0,
                room_stream_keepalive_seconds=3600,
            ),
        ),
        bot_name="Istota",
    )
    return cfg


def _revision(conn, group_id="team"):
    return db.get_group(conn, group_id, include_archived=True)["revision"]


def _change(conn, config, user, *, actor="alice", **kwargs):
    return service.change_membership(
        conn, config, "team", user, actor_id=actor,
        expected_revision=_revision(conn), **kwargs)


@pytest.fixture
def room(config):
    """`team` (alice owns it, bob is a member) and its group room, committed."""
    with db.get_db(config.db_path) as conn:
        service.create_group(conn, config, "team", actor_id="admin", is_admin=True,
                             expected_revision=0, owners=["alice"],
                             display_name="Team", kind="team")
        _change(conn, config, "bob", role="member", acknowledge_history=True)
        token = create_group_room(conn, "alice", "team", "Planning",
                                  "req-00000001")["room"].token
    return token


def _remove(config, user="bob"):
    with db.get_db(config.db_path) as conn:
        _change(conn, config, user, role=None)


def _archive(config):
    with db.get_db(config.db_path) as conn:
        service.archive_group(conn, "team", actor_id="admin", is_admin=True,
                              expected_revision=_revision(conn), confirm=True)


def _handle_id(config, token, user):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM web_chat_rooms WHERE token = ? AND user_id = ?",
            (token, user),
        ).fetchone()
    return row["id"] if row else None


def _task(config, token, user, status="pending", **fields):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt="hello", user_id=user,
                                 source_type="web", conversation_token=token,
                                 output_target="web")
        if status != "pending":
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        for column, value in fields.items():
            conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))
    return task_id


def _task_row(config, task_id):
    with db.get_db(config.db_path) as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def _post(config, token, body):
    with db.get_db(config.db_path) as conn:
        return db.add_message(conn, token, role="assistant", body=body,
                              origin_surface="web")


# ---------------------------------------------------------------------------
# The room-access resolver asks the live group
# ---------------------------------------------------------------------------


class TestTheResolverAsksTheGroup:
    def test_an_archived_group_suspends_its_room_with_the_roster_intact(self, config, room):
        _post(config, room, "before the archive")
        _archive(config)
        with db.get_db(config.db_path) as conn:
            # Nothing was emptied: the transcript and roster wait for recovery.
            assert db.list_room_members(conn, room) == ["alice", "bob"]
            for user in ("alice", "bob"):
                assert not db.is_room_member(conn, room, user)
                assert room not in [r.token for r in db.list_member_rooms(conn, user)]
                assert db.list_room_events_since(conn, user, since_id=0) == []
                assert db.list_messages_across_rooms(conn, user) == []
                assert db.total_unread_messages(conn, user, "web") == 0
            assert db.count_room_messages(conn, room) == 1

    def test_a_roster_row_written_around_the_group_admits_nobody(self, config, room):
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, room, "carol")
            db.add_message(conn, room, role="assistant", body="for the group",
                           origin_surface="web")
            assert not db.is_room_member(conn, room, "carol")
            assert db.list_member_rooms(conn, "carol") == []
            assert db.list_room_events_since(conn, "carol", since_id=0) == []

    def test_the_sql_and_the_access_predicate_agree(self, config, room):
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, room, "carol")
            for user in ("alice", "bob", "carol"):
                assert db.is_room_member(conn, room, user) == db.group_room_admits(conn, room, user)

    def test_a_linked_room_of_an_archived_group_keeps_its_roster(self, config, room):
        with db.get_db(config.db_path) as conn:
            linked = db.create_web_chat_room(conn, "alice", "Linked").token
            db.add_web_room_member(conn, linked, "carol")
            db.set_room_group(conn, linked, "team")
        _archive(config)
        with db.get_db(config.db_path) as conn:
            assert db.is_room_member(conn, linked, "carol")
            assert linked in [r.token for r in db.list_member_rooms(conn, "carol")]

    def test_a_removed_member_reaches_nothing_through_search_places_or_links(self, config, room):
        from istota.search.framework import _visible_rooms
        from istota.webui import app as web_app
        from istota.workspace.places import list_places

        (config.workspace_path / "Channels" / room).mkdir(parents=True)
        _post(config, room, "quarterly figures")
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, room, "bob")  # a row the group does not back
        _archive(config)
        web_app._config = config
        with db.get_db(config.db_path) as conn:
            assert db.search_messages(conn, "bob", '"quarterly"', limit=5, offset=0,
                                      exclude_tokens=set()) == []
            assert _visible_rooms(conn, "bob") == []
            assert f"room:{room}" not in [p.id for p in list_places(config, conn, "bob")]
        assert web_app._deep_link_target("bob", room, None) == "/istota/chat"


# ---------------------------------------------------------------------------
# Sends and stale handles
# ---------------------------------------------------------------------------


class TestNoResurrection:
    def test_a_turn_from_outside_the_group_is_dropped_and_rejoins_nobody(self, config, room):
        from istota.transport.ingest import record_inbound

        _remove(config)
        with db.get_db(config.db_path) as conn:
            before = db.count_room_messages(conn, room)
            result = record_inbound(
                conn, config, surface="web", surface_ref=room, user_id="bob",
                text="still here?", source_type="web", output_target="room",
                addressed_to_bot=True,
            )
            assert result.outcome == "dropped"
            assert result.task_id is None
            assert db.list_room_members(conn, room) == ["alice"]
            assert db.count_room_messages(conn, room) == before

    def test_a_member_turn_is_still_recorded(self, config, room):
        from istota.transport.ingest import record_inbound

        with db.get_db(config.db_path) as conn:
            result = record_inbound(
                conn, config, surface="web", surface_ref=room, user_id="bob",
                text="hello", source_type="web", output_target="room",
                addressed_to_bot=True,
            )
            assert result.outcome == "created"


@pytest.fixture
async def client(config):
    import istota.webui.app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@_needs_web
class TestTheChatRoutes:
    async def test_a_removed_members_stale_handle_cannot_read_or_send(self, client, config, room):
        bob = await _login(client, "bob")
        room_id = _handle_id(config, room, "bob")
        _remove(config)
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/messages", cookies=bob)
        assert resp.status_code == 404
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/messages",
                                 cookies=bob, headers=ORIGIN, json={"text": "back again"})
        assert resp.status_code == 404
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, room) == ["alice"]

    async def test_an_archived_groups_room_takes_no_reads_or_posts(self, client, config, room):
        alice = await _login(client, "alice")
        room_id = _handle_id(config, room, "alice")
        _archive(config)
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/messages", cookies=alice)
        assert resp.status_code == 404
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/messages",
                                 cookies=alice, headers=ORIGIN, json={"text": "anyone?"})
        assert resp.status_code == 404
        listed = await client.get("/istota/api/chat/rooms", cookies=alice)
        assert room not in [r["token"] for r in listed.json()["rooms"]]
        with db.get_db(config.db_path) as conn:
            assert db.count_room_messages(conn, room) == 0
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@_needs_web
class TestHideIsNotLeave:
    @pytest.mark.parametrize("user", ["alice", "bob"])
    async def test_hiding_keeps_the_member_in_the_room(self, client, config, room, user):
        cookies = await _login(client, user)
        room_id = _handle_id(config, room, user)
        resp = await client.patch(f"/istota/api/chat/rooms/{room_id}", cookies=cookies,
                                  headers=ORIGIN, json={"archived": True})
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, room) == ["alice", "bob"]
            assert db.is_room_member(conn, room, user)
            assert db.is_room_dismissed(conn, room, user)
            # Never the global flag: the other member still has the room.
            assert not db.get_room(conn, room).archived
            other = "bob" if user == "alice" else "alice"
            assert room in [r.token for r in db.list_member_rooms(conn, other)]
            assert f"u:{user}" in db.audience_persons(conn, room)
            # Somebody joining the group does not undo the hide.
            _change(conn, config, "carol", role="member", acknowledge_history=True)
            assert db.is_room_dismissed(conn, room, user)
            assert db.list_room_members(conn, room) == ["alice", "bob", "carol"]

    async def test_unhiding_brings_it_back(self, client, config, room):
        cookies = await _login(client, "bob")
        room_id = _handle_id(config, room, "bob")
        for archived in (True, False):
            await client.patch(f"/istota/api/chat/rooms/{room_id}", cookies=cookies,
                               headers=ORIGIN, json={"archived": archived})
        with db.get_db(config.db_path) as conn:
            assert room in [r.token for r in db.list_member_rooms(conn, "bob")]

    @pytest.mark.parametrize("user", ["alice", "bob"])
    async def test_delete_and_leave_are_refused(self, client, config, room, user):
        cookies = await _login(client, user)
        room_id = _handle_id(config, room, user)
        resp = await client.delete(f"/istota/api/chat/rooms/{room_id}",
                                   cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "group_managed"
        resp = await client.delete(f"/istota/api/chat/rooms/{room_id}/members/{user}",
                                   cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "group_managed"
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, room) is not None
            assert db.list_room_members(conn, room) == ["alice", "bob"]
            assert _handle_id(config, room, user) == room_id

    async def test_an_ordinary_rooms_delete_is_unchanged(self, client, config, room):
        with db.get_db(config.db_path) as conn:
            mine = db.create_web_chat_room(conn, "alice", "Mine")
        cookies = await _login(client, "alice")
        resp = await client.delete(f"/istota/api/chat/rooms/{mine.id}",
                                   cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, mine.token) is None


# ---------------------------------------------------------------------------
# Streams stop on the next batch
# ---------------------------------------------------------------------------


class _Request:
    """A Request stand-in for driving a stream generator in-process.

    ``on_check`` runs once per loop iteration, between batches, which is where
    a removal lands in a live stream.
    """

    def __init__(self, username, *, disconnect_after, on_check=None, headers=None):
        self.headers = headers or {}
        self.session = {"user": {"username": username}}
        self.state = SimpleNamespace()
        self._checks = 0
        self._limit = disconnect_after
        self._on_check = on_check

    async def is_disconnected(self) -> bool:
        self._checks += 1
        if self._on_check is not None:
            self._on_check(self._checks)
        return self._checks > self._limit


async def _read(response) -> str:
    out = ""
    async for chunk in response.body_iterator:
        out += chunk if isinstance(chunk, str) else chunk.decode()
    return out


@_needs_web
class TestTheRoomStream:
    async def _stream(self, config, username, on_check, disconnect_after=5):
        import istota.webui.app as mod
        mod._config = config
        mod.app.state.istota_config = config
        request = _Request(username, disconnect_after=disconnect_after, on_check=on_check)
        resp = await mod.chat_room_stream(request, since_id=0, user={"username": username})
        return await _read(resp)

    async def test_a_removal_during_the_stream_stops_the_next_batch(self, config, room):
        posted = {}

        def on_check(n):
            if n == 1:
                posted["before"] = _post(config, room, "before the removal")
            if n == 3:
                _remove(config)
                posted["after"] = _post(config, room, "after the removal")

        buf = await self._stream(config, "bob", on_check)
        assert "before the removal" in buf
        assert f"id: {posted['before']}" in buf
        assert "after the removal" not in buf
        assert f"id: {posted['after']}" not in buf

    async def test_an_archive_during_the_stream_stops_it_too(self, config, room):
        def on_check(n):
            if n == 2:
                _archive(config)
                _post(config, room, "after the archive")

        buf = await self._stream(config, "alice", on_check)
        assert "after the archive" not in buf

    async def test_the_member_who_stays_keeps_reading(self, config, room):
        def on_check(n):
            if n == 2:
                _remove(config)
                _post(config, room, "for the ones still here")

        assert "for the ones still here" in await self._stream(config, "alice", on_check)


@_needs_web
class TestTheTaskStream:
    async def test_a_removal_mid_task_stops_its_stream(self, config, room):
        import istota.webui.app as mod
        mod._config = config
        task_id = _task(config, room, "bob", status="running")

        def emit(seq, text):
            with db.get_db(config.db_path) as conn:
                conn.execute(
                    "INSERT INTO task_events (task_id, seq, kind, payload) "
                    "VALUES (?, ?, 'text_delta', ?)",
                    (task_id, seq, json.dumps({"text": text})),
                )

        def on_check(n):
            if n == 1:
                emit(1, "early words")
            if n == 3:
                _remove(config)
                emit(2, "words after the removal")

        request = _Request("bob", disconnect_after=6, on_check=on_check)
        resp = await mod.chat_task_stream(task_id, request, since_seq=0,
                                          user={"username": "bob"})
        buf = await _read(resp)
        assert "early words" in buf
        assert "words after the removal" not in buf
        # Ended by the refusal, not by the client going away.
        assert request._checks < 6

    async def test_the_events_snapshot_is_refused_after_removal(self, client, config, room):
        task_id = _task(config, room, "bob", status="running")
        bob = await _login(client, "bob")
        assert (await client.get(f"/istota/api/chat/tasks/{task_id}/events",
                                 cookies=bob)).status_code == 200
        _remove(config)
        assert (await client.get(f"/istota/api/chat/tasks/{task_id}/events",
                                 cookies=bob)).status_code == 404


# ---------------------------------------------------------------------------
# A removed member's unfinished tasks
# ---------------------------------------------------------------------------


class TestTheRemovedMembersTasks:
    def test_pending_tasks_are_cancelled_and_running_ones_flagged(self, config, room):
        pending = _task(config, room, "bob")
        parked = _task(config, room, "bob", status="pending_confirmation")
        running = _task(config, room, "bob", status="running")
        others = _task(config, room, "alice")
        _remove(config)
        assert _task_row(config, pending)["status"] == "cancelled"
        assert _task_row(config, parked)["status"] == "cancelled"
        assert _task_row(config, running)["status"] == "running"
        assert _task_row(config, running)["cancel_requested"] == 1
        assert _task_row(config, others)["status"] == "pending"
        assert _task_row(config, others)["cancel_requested"] == 0

    def test_an_archive_stops_everyones_tasks_in_its_rooms(self, config, room):
        tasks = [_task(config, room, "alice"), _task(config, room, "bob")]
        with db.get_db(config.db_path) as conn:
            elsewhere = db.create_web_chat_room(conn, "alice", "Mine").token
        unrelated = _task(config, elsewhere, "alice")
        _archive(config)
        assert [_task_row(config, t)["status"] for t in tasks] == ["cancelled"] * 2
        assert _task_row(config, unrelated)["status"] == "pending"

    def test_a_running_task_is_killed_after_the_commit(self, config, room, monkeypatch):
        """A real process group, signalled through the scheduler's own path,
        and only once the membership change has committed."""
        from istota.sandbox import process_group

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
        try:
            task_id = _task(config, room, "bob", status="running", worker_pid=proc.pid)
            lock_free = []
            real = process_group.signal_task_worker

            def spy(pid, sig=signal.SIGTERM):
                probe = sqlite3.connect(config.db_path, timeout=0)
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    probe.rollback()
                    lock_free.append(True)
                except sqlite3.OperationalError:
                    lock_free.append(False)
                finally:
                    probe.close()
                return real(pid, sig)

            monkeypatch.setattr(process_group, "signal_task_worker", spy)
            process_group.own_task_pids(True)
            try:
                _remove(config)
            finally:
                process_group.own_task_pids(False)
            assert proc.wait(timeout=10) == -signal.SIGTERM
            assert lock_free == [True]
            assert _task_row(config, task_id)["cancel_requested"] == 1
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_no_signal_outside_the_scheduler_process(self, config, room):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
        try:
            task_id = _task(config, room, "bob", status="running", worker_pid=proc.pid)
            _remove(config)
            # The web process cannot reach the daemon's pids; the flag carries it.
            assert proc.poll() is None
            assert _task_row(config, task_id)["cancel_requested"] == 1
        finally:
            proc.kill()
            proc.wait()


def _brain(answer, during=None):
    def run(task, config, user_resources, dry_run=False, event_writer=None, **_kw):
        if during is not None:
            during()
        return True, answer, None, None
    return run


def _run(config, brain):
    from istota.scheduler import process_one_task

    with ExitStack() as stack:
        mock = stack.enter_context(patch("istota.scheduler.execute_task", side_effect=brain))
        result = process_one_task(config)
    return result, mock


class TestDeliveryRefusal:
    def test_an_answer_finishing_after_the_removal_is_not_delivered(self, config, room):
        task_id = _task(config, room, "bob")
        (ran, success), brain = _run(
            config, _brain("the group's private plan", during=lambda: _remove(config)))
        assert ran == task_id and not success
        assert brain.called
        row = _task_row(config, task_id)
        assert row["status"] == "cancelled"
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE task_id = ? AND role = 'assistant'",
                (task_id,),
            ).fetchone()[0] == 0
            events = db.get_task_events(conn, task_id, 0)
        assert "the group's private plan" not in json.dumps([e["payload"] for e in events])
        assert "result" not in [e["kind"] for e in events]

    def test_a_removal_after_the_first_check_still_stores_nothing(self, config, room):
        # The removal commits after the post-run check has passed and before
        # the completion transaction stores the answer (ISSUE-813).
        from istota import scheduler

        task_id = _task(config, room, "bob")
        real_plan = scheduler.resolve_delivery_plan

        def plan_after_removal(*args, **kwargs):
            _remove(config)
            return real_plan(*args, **kwargs)

        with patch("istota.scheduler.resolve_delivery_plan", side_effect=plan_after_removal):
            (ran, success), brain = _run(config, _brain("the group's private plan"))
        assert ran == task_id and not success
        assert brain.called
        assert _task_row(config, task_id)["status"] == "cancelled"
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE task_id = ? AND role = 'assistant'",
                (task_id,),
            ).fetchone()[0] == 0
            events = db.get_task_events(conn, task_id, 0)
        assert "the group's private plan" not in json.dumps([e["payload"] for e in events])

    def test_memory_indexing_runs_after_the_completion_transaction(self, config, room):
        # `index_conversation` commits its connection. Run inside the
        # completion transaction, it ended the transaction the delivery check
        # holds, and a removal could commit before the rows stored after it.
        from istota.memory import search

        task_id = _task(config, room, "alice")
        real_index = search.index_conversation
        seen = []

        def index_after_store(conn, *args, **kwargs):
            with db.get_db(config.db_path) as other:
                seen.append(other.execute(
                    "SELECT COUNT(*) FROM messages WHERE task_id = ? AND role = 'assistant'",
                    (task_id,),
                ).fetchone()[0])
            return real_index(conn, *args, **kwargs)

        with patch("istota.memory.search.index_conversation", side_effect=index_after_store):
            (ran, success), _ = _run(config, _brain("noted"))
        assert ran == task_id and success
        assert seen and all(count == 1 for count in seen)

    def test_a_task_whose_author_left_does_not_run(self, config, room):
        task_id = _task(config, room, "bob")
        with db.get_db(config.db_path) as conn:
            # Membership ended by a path that did not cancel the task.
            db.end_group_membership(conn, "team", "bob", ended_by="alice")
        (ran, success), brain = _run(config, _brain("unused"))
        assert ran == task_id and not success
        assert not brain.called
        assert _task_row(config, task_id)["status"] == "cancelled"

    def test_a_staying_members_answer_is_delivered(self, config, room):
        task_id = _task(config, room, "alice")
        (ran, success), _ = _run(config, _brain("noted", during=lambda: _remove(config)))
        assert ran == task_id and success
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT body FROM messages WHERE task_id = ? AND role = 'assistant'",
                (task_id,),
            ).fetchone()[0] == "noted"


class TestAudienceEpochs:
    def test_removal_and_rejoin_split_no_epoch(self, config, room):
        with db.get_db(config.db_path) as conn:
            _change(conn, config, "bob", role=None)
            _change(conn, config, "bob", role="member", acknowledge_history=True)
            assert conn.execute(
                "SELECT COUNT(*) FROM room_epochs WHERE room_token = ? AND epoch > 0",
                (room,),
            ).fetchone()[0] == 0
            assert not db.front_stage_cutoff(conn, room)


# ---------------------------------------------------------------------------
# A group room is never anyone's private room
# ---------------------------------------------------------------------------


class TestAGroupRoomIsNeverPrivate:
    """A one-member group room is still read by every future member, so
    nothing that delivers to "the user's own room" may pick it."""

    @pytest.fixture
    def solo(self, config):
        with db.get_db(config.db_path) as conn:
            service.create_group(conn, config, "solo", actor_id="admin", is_admin=True,
                                 expected_revision=0, owners=["carol"],
                                 display_name="Solo", kind="team")
            token = create_group_room(conn, "carol", "solo", "Plans",
                                      "req-00000009")["room"].token
        return token

    def test_the_default_room_and_the_private_predicate_refuse_it(self, config, solo):
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, solo) == ["carol"]
            handle = db.default_web_room(conn, "carol")
            assert handle is None or handle.token != solo
            assert not db.is_private_room_of(conn, solo, "carol")
            assert not db.is_private_room_of(conn, solo, "carol", allow_phone=True)

    def test_a_private_general_is_still_minted(self, config, solo):
        with db.get_db(config.db_path) as conn:
            handle = db.ensure_default_web_chat_room(conn, "carol")
            assert handle.token != solo
            assert db.is_private_room_of(conn, handle.token, "carol")
            assert db.default_web_room(conn, "carol").token == handle.token

    def test_private_replies_and_relay_questions_skip_it(self, config, solo):
        from istota.relay.destinations import RequestError, _room
        from istota.rooms.private_replies import private_room_for

        with db.get_db(config.db_path) as conn:
            assert private_room_for(conn, config, "carol", solo) is None
            with pytest.raises(RequestError):
                _room(conn, config, "carol")


class TestARemovedMemberCannotReachBack:
    """Commands that take a task id run from anywhere, so a removed member's
    private room must not become a way back into the group room."""

    @pytest.mark.parametrize("resume", [False, True])
    def test_retry_and_resume_are_refused(self, config, room, resume):
        from istota import commands

        task_id = _task(config, room, "bob", status="failed")
        _remove(config)
        with db.get_db(config.db_path) as conn:
            before = db.count_room_messages(conn, room)
            tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = commands.retry_task(conn, config, "bob", db.get_task(conn, task_id),
                                          resume=resume, surface="web")
            assert isinstance(outcome, str)
            assert "group" in outcome
            assert db.count_room_messages(conn, room) == before
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_before

    def test_a_staying_member_can_still_retry(self, config, room):
        from istota import commands

        task_id = _task(config, room, "alice", status="failed")
        _remove(config)
        with db.get_db(config.db_path) as conn:
            outcome = commands.retry_task(conn, config, "alice", db.get_task(conn, task_id),
                                          surface="web")
            assert isinstance(outcome, commands.RetryOutcome)

    async def test_more_does_not_show_the_trace(self, config, room):
        from istota import commands

        trace = json.dumps([{"type": "text", "text": "the private plan"}])
        task_id = _task(config, room, "bob", status="failed", execution_trace=trace)
        _remove(config)
        with db.get_db(config.db_path) as conn:
            own = db.create_web_chat_room(conn, "bob", "Mine").token
            ctx = commands.CommandContext(
                config=config, conn=conn, user_id="bob", conversation_token=own,
                surface="web", args=f"#{task_id}",
            )
            reply = await commands.cmd_more(ctx)
        assert "the private plan" not in str(reply)
        assert "group" in str(reply)


class TestAnAdminOutsideTheGroup:
    """Being an admin grants no group membership (ISSUE-811): the actor must be
    let into the group room as well as the task's author."""

    @pytest.mark.parametrize("resume", [False, True])
    def test_retry_and_resume_are_refused(self, config, room, resume):
        from istota import commands

        config.admin_users = {"carol"}
        task_id = _task(config, room, "bob", status="failed")
        with db.get_db(config.db_path) as conn:
            before = db.count_room_messages(conn, room)
            tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = commands.retry_task(conn, config, "carol", db.get_task(conn, task_id),
                                          resume=resume, surface="web")
            assert isinstance(outcome, str)
            assert "you are not a member" in outcome
            assert db.count_room_messages(conn, room) == before
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_before

    async def test_more_does_not_show_the_trace(self, config, room):
        from istota import commands

        config.admin_users = {"carol"}
        trace = json.dumps([{"type": "text", "text": "the private plan"}])
        task_id = _task(config, room, "bob", status="failed", execution_trace=trace)
        with db.get_db(config.db_path) as conn:
            own = db.create_web_chat_room(conn, "carol", "Mine").token
            ctx = commands.CommandContext(
                config=config, conn=conn, user_id="carol", conversation_token=own,
                surface="web", args=f"#{task_id}",
            )
            reply = await commands.cmd_more(ctx)
        assert "the private plan" not in str(reply)
        assert "you are not a member" in str(reply)

    @_needs_web
    async def test_the_web_task_routes_refuse(self, client, config, room):
        config.admin_users = {"carol", "alice"}
        task_id = _task(config, room, "bob", status="failed")
        with db.get_db(config.db_path) as conn:
            before = db.count_room_messages(conn, room)
            tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        carol = await _login(client, "carol")
        assert (await client.get(f"/istota/api/chat/tasks/{task_id}/events",
                                 cookies=carol)).status_code == 404
        retry = await client.post(f"/istota/api/chat/tasks/{task_id}/retry",
                                  cookies=carol, headers=ORIGIN, json={"mode": "retry"})
        assert retry.status_code == 404
        with db.get_db(config.db_path) as conn:
            assert db.count_room_messages(conn, room) == before
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_before
        alice = await _login(client, "alice")
        assert (await client.get(f"/istota/api/chat/tasks/{task_id}/events",
                                 cookies=alice)).status_code == 200

    @_needs_web
    async def test_an_admin_outside_the_group_can_still_stop_a_task(self, client, config, room):
        """Cancel keeps the operator's reach `!stop` has: stopping writes nothing
        into the room."""
        config.admin_users = {"carol"}
        task_id = _task(config, room, "bob", status="running")
        carol = await _login(client, "carol")
        resp = await client.post(f"/istota/api/chat/tasks/{task_id}/cancel",
                                 cookies=carol, headers=ORIGIN)
        assert resp.status_code == 200
        assert _task_row(config, task_id)["cancel_requested"] == 1

    def test_an_admin_in_the_group_can_still_retry(self, config, room):
        from istota import commands

        config.admin_users = {"alice"}
        task_id = _task(config, room, "bob", status="failed")
        with db.get_db(config.db_path) as conn:
            outcome = commands.retry_task(conn, config, "alice", db.get_task(conn, task_id),
                                          surface="web")
            assert isinstance(outcome, commands.RetryOutcome)


class TestALostHostIsSucceeded:
    """The host's web identity disabled: the access check drops them on the
    next read, and an owner who is still in the room takes over there and
    then, not at the next membership change."""

    @pytest.fixture
    def two_owners(self, config):
        with db.get_db(config.db_path) as conn:
            service.create_group(conn, config, "team", actor_id="admin", is_admin=True,
                                 expected_revision=0, owners=["alice", "carol"],
                                 display_name="Team", kind="team")
            _change(conn, config, "bob", role="member", acknowledge_history=True)
            token = create_group_room(conn, "alice", "team", "Planning",
                                      "req-00000002")["room"].token
        return token

    def test_a_disabled_hosts_room_takes_the_next_owner(self, config, two_owners):
        from istota.rooms import policy as room_policy
        from istota.rooms import speech_gate
        from istota.transport.ingest import record_inbound

        with db.get_db(config.db_path) as conn:
            assert room_policy.get_policy(conn, two_owners).host_user_id == "alice"
            conn.execute(
                "INSERT INTO web_auth_identities (user_id, email, disabled) "
                "VALUES ('alice', 'alice@example.com', 1)")
            result = record_inbound(
                conn, config, surface="web", surface_ref=two_owners, user_id="bob",
                text="what's the plan?", source_type="web", output_target="room",
                addressed_to_bot=True,
            )
            rung = conn.execute(
                "SELECT rung FROM speech_gate_decisions WHERE message_id = ?",
                (result.message_id,),
            ).fetchone()
            assert rung is None or rung[0] != speech_gate.RUNG_HOST_LOST
            assert result.outcome == "created"
            assert room_policy.get_policy(conn, two_owners).host_user_id == "carol"
