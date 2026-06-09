"""Tests for the web chat surface (Phase 1 backend)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import (
    Config,
    SiteConfig,
    UserConfig,
    WebChatConfig,
    WebConfig,
    load_config,
)
from istota.transport.registry import make_registry
from istota.transport.routing import plan_has_surface, resolve_delivery_plan

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# _trace_segments — ordered history segment reconstruction
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestTraceSegments:
    def _fn(self):
        from istota.web_app import _trace_segments
        return _trace_segments

    def test_ordered_trace_skips_cm_boundary_and_canonicalizes_answer(self):
        import json
        trace = json.dumps([
            {"type": "text", "text": "Let me check."},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "calendar list"},
            {"type": "text", "text": "draft answer"},
        ])
        segs = self._fn()(trace, None, "final answer")
        assert segs == [
            {"kind": "text", "text": "Let me check."},
            {"kind": "tool", "text": "calendar list"},
            {"kind": "text", "text": "final answer"},
        ]

    def test_trace_ending_in_tool_appends_result(self):
        import json
        trace = json.dumps([
            {"type": "text", "text": "narration"},
            {"type": "tool", "text": "ran a thing"},
        ])
        segs = self._fn()(trace, None, "the answer")
        assert segs == [
            {"kind": "text", "text": "narration"},
            {"kind": "tool", "text": "ran a thing"},
            {"kind": "text", "text": "the answer"},
        ]

    def test_no_trace_falls_back_to_actions_taken(self):
        import json
        actions = json.dumps(["Read a.txt", "Grep b"])
        segs = self._fn()(None, actions, "result text")
        assert segs == [
            {"kind": "tool", "text": "Read a.txt"},
            {"kind": "tool", "text": "Grep b"},
            {"kind": "text", "text": "result text"},
        ]

    def test_neither_trace_nor_actions_returns_result_only(self):
        assert self._fn()(None, None, "just the answer") == [
            {"kind": "text", "text": "just the answer"},
        ]

    def test_empty_result_with_nothing_is_empty(self):
        assert self._fn()(None, None, "") == []
        assert self._fn()(None, None, None) == []

    def test_malformed_trace_falls_back_without_raising(self):
        segs = self._fn()("{not json", '["Tool A"]', "answer")
        assert segs == [
            {"kind": "tool", "text": "Tool A"},
            {"kind": "text", "text": "answer"},
        ]

    def test_empty_result_keeps_trace_text(self):
        import json
        trace = json.dumps([{"type": "text", "text": "streamed"}])
        # An empty result leaves the trace's trailing text as the answer.
        assert self._fn()(trace, None, "") == [
            {"kind": "text", "text": "streamed"},
        ]


# ---------------------------------------------------------------------------
# DB layer: rooms + rate-limit counter
# ---------------------------------------------------------------------------


class TestWebChatRoomsDB:
    def test_ensure_default_creates_general(self, conn):
        room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.name == "general"
        assert room.user_id == "alice"
        assert room.token.startswith("web-alice-")
        assert not room.archived

    def test_ensure_default_idempotent(self, conn):
        first = db.ensure_default_web_chat_room(conn, "alice")
        second = db.ensure_default_web_chat_room(conn, "alice")
        assert first.id == second.id

    def test_create_and_list_rooms_oldest_first(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "alice", "ideas")
        rooms = db.list_web_chat_rooms(conn, "alice")
        assert [r.name for r in rooms] == ["general", "ideas"]

    def test_rooms_are_per_user(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "bob", "general")
        assert len(db.list_web_chat_rooms(conn, "alice")) == 1
        assert len(db.list_web_chat_rooms(conn, "bob")) == 1

    def test_tokens_are_unique(self, conn):
        a = db.create_web_chat_room(conn, "alice", "one")
        b = db.create_web_chat_room(conn, "alice", "two")
        assert a.token != b.token

    def test_rename_room(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        updated = db.update_web_chat_room(conn, room.id, name="renamed")
        assert updated.name == "renamed"

    def test_archive_hides_from_default_list(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.update_web_chat_room(conn, room.id, archived=True)
        assert db.list_web_chat_rooms(conn, "alice") == []
        assert len(db.list_web_chat_rooms(conn, "alice", include_archived=True)) == 1

    def test_get_by_token(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        found = db.get_web_chat_room_by_token(conn, room.token)
        assert found.id == room.id

    def test_count_recent_web_tasks(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        for _ in range(3):
            db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room.token, output_target="web",
            )
        # A non-web task for the same user must not be counted.
        db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
        assert db.count_recent_web_tasks(conn, "alice", 300) == 3


class TestWebChatMessagesDB:
    """Bot-delivered (unsolicited) room messages — the `web` delivery surface."""

    def test_add_and_list_oldest_first(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.add_web_chat_message(conn, "alice", room.token, "first")
        db.add_web_chat_message(conn, "alice", room.token, "second", title="T")
        msgs = db.list_web_chat_messages(conn, room.token)
        assert [m.text for m in msgs] == ["first", "second"]
        assert msgs[0].role == "system"
        assert msgs[1].title == "T"

    def test_add_returns_id(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        mid = db.add_web_chat_message(conn, "alice", room.token, "x")
        assert isinstance(mid, int) and mid > 0

    def test_scoped_by_token(self, conn):
        a = db.create_web_chat_room(conn, "alice", "one")
        b = db.create_web_chat_room(conn, "alice", "two")
        db.add_web_chat_message(conn, "alice", a.token, "in-a")
        assert [m.text for m in db.list_web_chat_messages(conn, a.token)] == ["in-a"]
        assert db.list_web_chat_messages(conn, b.token) == []

    def test_limit_keeps_most_recent(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        for i in range(5):
            db.add_web_chat_message(conn, "alice", room.token, f"m{i}")
        msgs = db.list_web_chat_messages(conn, room.token, limit=2)
        assert [m.text for m in msgs] == ["m3", "m4"]


def _seed_task_event(conn, task_id: int, seq: int = 1) -> None:
    """Insert a bare task_events row for a task (mirrors EventWriter.emit)."""
    conn.execute(
        "INSERT INTO task_events (task_id, seq, kind, payload, created_at) "
        "VALUES (?, ?, 'result', '{}', datetime('now'))",
        (task_id, seq),
    )


class TestWebChatRoomDelete:
    """Hard delete + cascade across every table keyed on a room's token."""

    def test_delete_removes_room(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        assert db.delete_web_chat_room(conn, room.id, "alice") is True
        assert db.list_web_chat_rooms(conn, "alice") == []

    def test_delete_cascades_tasks_and_events(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "keep")
        tid = db.create_task(
            conn, prompt="hi", user_id="alice", source_type="web",
            conversation_token=room.token, output_target="web",
        )
        _seed_task_event(conn, tid)
        # A task in another room must survive.
        keep_tid = db.create_task(
            conn, prompt="stay", user_id="alice", source_type="web",
            conversation_token=other.token, output_target="web",
        )
        _seed_task_event(conn, keep_tid)

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.get_task(conn, tid) is None
        assert db.get_task_events(conn, tid) == []
        assert db.get_task(conn, keep_tid) is not None
        assert len(db.get_task_events(conn, keep_tid)) == 1

    def test_delete_cascades_web_chat_messages(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "keep")
        db.add_web_chat_message(conn, "alice", room.token, "gone")
        db.add_web_chat_message(conn, "alice", other.token, "stays")

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.list_web_chat_messages(conn, room.token) == []
        assert [m.text for m in db.list_web_chat_messages(conn, other.token)] == ["stays"]

    def test_delete_cascades_channel_sleep_state(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.set_channel_sleep_cycle_last_run(conn, room.token, None)
        assert db.get_channel_sleep_cycle_last_run(conn, room.token)[0] is not None

        assert db.delete_web_chat_room(conn, room.id, "alice") is True

        assert db.get_channel_sleep_cycle_last_run(conn, room.token)[0] is None

    def test_delete_wrong_user_returns_false(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        assert db.delete_web_chat_room(conn, room.id, "bob") is False
        assert len(db.list_web_chat_rooms(conn, "alice")) == 1

    def test_delete_unknown_id_returns_false(self, conn):
        assert db.delete_web_chat_room(conn, 9999, "alice") is False

    def test_count_active_web_tasks(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        other = db.create_web_chat_room(conn, "alice", "other")
        # Two non-terminal tasks on the room token.
        for _ in range(2):
            db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room.token, output_target="web",
            )
        # A terminal task on the same token must not be counted.
        done = db.create_task(
            conn, prompt="done", user_id="alice", source_type="web",
            conversation_token=room.token, output_target="web",
        )
        db.update_task_status(conn, done, "completed", result="ok")
        # A task in another room must not be counted.
        db.create_task(
            conn, prompt="elsewhere", user_id="alice", source_type="web",
            conversation_token=other.token, output_target="web",
        )

        assert db.count_active_web_tasks(conn, room.token, "alice") == 2

    def test_count_active_web_tasks_includes_foreign_push(self, conn):
        # An email reply routed INTO a room (source_type="email") targets the
        # room via conversation_token and will write to it — the busy-room
        # delete guard must count it, not just source_type="web" tasks.
        room = db.create_web_chat_room(conn, "alice", "general")
        db.create_task(
            conn, prompt="email reply", user_id="alice", source_type="email",
            conversation_token=room.token, output_target=f"web:{room.token},email",
        )
        assert db.count_active_web_tasks(conn, room.token, "alice") == 1


# ---------------------------------------------------------------------------
# Delivery routing: web is a stream surface (no Talk/email push)
# ---------------------------------------------------------------------------


class TestWebDeliveryRouting:
    def _config(self, tmp_path):
        return Config(db_path=tmp_path / "istota.db")

    def test_web_output_target_resolves_to_stream(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target="web",
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")
        assert not plan_has_surface(plan, "email")
        assert all(d.kind == "stream" for d in plan)

    def test_web_default_plan_when_target_unset(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target=None,
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestWebChatConfig:
    def test_defaults(self):
        chat = WebChatConfig()
        assert chat.max_prompt_chars == 32000
        assert chat.rate_limit_messages == 30
        assert "pdf" in chat.attachment_extensions

    def test_parsed_from_toml(self, tmp_path):
        toml = tmp_path / "config.toml"
        toml.write_text(
            "[web]\nenabled = true\n\n"
            "[web.chat]\nmax_prompt_chars = 1000\nrate_limit_messages = 5\n"
        )
        config = load_config(toml)
        assert config.web.chat.max_prompt_chars == 1000
        assert config.web.chat.rate_limit_messages == 5
        # Untouched knobs keep defaults.
        assert config.web.chat.max_attachment_mb == 25

    @_needs_web_deps
    def test_sse_poll_interval_wired(self, tmp_path):
        """The SSE generator's poll cadence must come from config, not a
        hardcoded constant."""
        import istota.web_app as mod
        config = _make_config(tmp_path)
        config.web.chat.sse_poll_interval_ms = 750
        mod._config = config
        assert mod._sse_poll_seconds() == 0.75


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def chat_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


@_needs_web_deps
class TestChatRoomsApi:
    async def test_rooms_requires_auth(self, chat_client):
        resp = await chat_client.get("/istota/api/chat/rooms")
        assert resp.status_code == 401

    async def test_list_rooms_autocreates_general(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/rooms", cookies=cookies)
        assert resp.status_code == 200
        rooms = resp.json()["rooms"]
        assert len(rooms) == 1
        assert rooms[0]["name"] == "general"

    async def test_create_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "ideas"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "ideas"

    async def test_rename_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "old"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "new"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new"

    async def test_cannot_touch_other_users_room(self, chat_client):
        alice = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "secret"}, cookies=alice,
            headers={"origin": "https://example.com"},
        )).json()
        bob = await _login(chat_client, "bob")
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "x"},
            cookies=bob, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404


@_needs_web_deps
class TestChatMessagesApi:
    async def _room(self, client, cookies):
        return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"][0]

    async def test_send_creates_web_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "what's on my calendar?"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is not None
        assert "stream_url" in body
        # The task is a source_type=web task on the room token.
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, body["task_id"])
        assert task.source_type == "web"
        assert task.output_target == "web"
        assert task.conversation_token == room["token"]

    async def test_empty_text_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "   "}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_history_round_trip(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hello there"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["text"] == "hello there"
        assert data["active_task"] is not None  # task is pending

    async def test_delivered_notification_surfaces_in_history(self, chat_client):
        """A message posted to the `web` surface (alert / log) shows up in the
        room transcript as a system message with a stable notif_id and no
        task_id — the user sees it on the next room load."""
        from istota import db
        from istota.web_app import _config
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        with db.get_db(_config.db_path) as conn:
            db.add_web_chat_message(
                conn, "alice", room["token"], "disk almost full", title="Alert",
            )
        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        sys_msgs = [m for m in data["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1
        assert "disk almost full" in sys_msgs[0]["text"]
        assert "Alert" in sys_msgs[0]["text"]
        assert "notif_id" in sys_msgs[0]
        assert "task_id" not in sys_msgs[0]

    async def test_completed_task_history_carries_trace_and_duration(self, chat_client):
        """A completed web task surfaces its tool trace and wall-clock duration
        in history, so the action strip and timing persist as an inspectable
        done state across reloads / room switches (ISSUE-122)."""
        import json

        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        trace = json.dumps([
            {"type": "tool", "text": "Read config.toml"},
            {"type": "text", "text": "thinking"},
            {"type": "tool", "text": "Grep for foo"},
        ])
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="do the thing", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(
                conn, tid, "completed", result="done!",
                actions_taken=json.dumps(["Read config.toml", "Grep for foo"]),
                execution_trace=trace,
            )
            # Stamp a deterministic 7-second wall clock.
            conn.execute(
                "UPDATE tasks SET started_at = '2026-06-07 10:00:00', "
                "completed_at = '2026-06-07 10:00:07' WHERE id = ?",
                (tid,),
            )

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["text"] == "done!"
        # Tool descriptions persist (in order) so the action strip can rebuild.
        assert assistant["tools"] == ["Read config.toml", "Grep for foo"]
        assert assistant["duration_seconds"] == 7.0
        # Ordered, interleaved segments reconstruct the live layout: tool, the
        # mid-turn narration, tool, then the canonical answer as a trailing text.
        assert assistant["segments"] == [
            {"kind": "tool", "text": "Read config.toml"},
            {"kind": "text", "text": "thinking"},
            {"kind": "tool", "text": "Grep for foo"},
            {"kind": "text", "text": "done!"},
        ]

    async def test_completed_task_history_carries_model(self, chat_client):
        """A completed web task surfaces the model that produced it, so the
        chat-message meta shows it on reload (verification-added test)."""
        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(conn, tid, "completed", result="hello")
            db.set_task_model_used(conn, tid, "claude-opus-4-8")

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["model"] == "claude-opus-4-8"

    async def test_history_completed_task_without_model_returns_null(self, chat_client):
        """A completed web task with no recorded model returns model=None, not
        an error or a missing key (verification-added test)."""
        import istota.web_app as mod

        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room["token"], output_target="web",
            )
            db.update_task_status(conn, tid, "completed", result="hello")

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()
        assistant = next(
            m for m in data["messages"]
            if m["role"] == "assistant" and m.get("task_id") == tid
        )
        assert assistant["model"] is None

    async def test_history_multiple_in_flight_ordered(self, chat_client):
        """Several queued messages each surface a user msg + an in-flight
        assistant placeholder, and active_tasks lists them oldest-first so the
        client can resume one and queue the rest in order."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        task_ids = []
        for text in ("first", "second", "third"):
            r = await chat_client.post(
                f"/istota/api/chat/rooms/{room['id']}/messages",
                json={"text": text}, cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            task_ids.append(r.json()["task_id"])

        data = (await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )).json()

        # Oldest-first, all three in-flight.
        assert [t["id"] for t in data["active_tasks"]] == task_ids
        assert data["active_task"]["id"] == task_ids[0]

        # Each task contributes a user message and an in-flight assistant slot,
        # interleaved in order.
        roles = [(m["role"], m["task_id"]) for m in data["messages"]]
        assert roles == [
            ("user", task_ids[0]), ("assistant", task_ids[0]),
            ("user", task_ids[1]), ("assistant", task_ids[1]),
            ("user", task_ids[2]), ("assistant", task_ids[2]),
        ]
        assistants = [m for m in data["messages"] if m["role"] == "assistant"]
        assert all(m["text"] == "" and m["status"] == "pending" for m in assistants)

    async def test_rate_limit_returns_429(self, chat_client):
        import istota.web_app as mod
        mod._config.web.chat.rate_limit_messages = 2
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        for _ in range(2):
            ok = await chat_client.post(
                f"/istota/api/chat/rooms/{room['id']}/messages",
                json={"text": "hi"}, cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            assert ok.status_code == 200
        blocked = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hi"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        mod._config.web.chat.rate_limit_messages = 30

    async def test_send_to_archived_room_rejected(self, chat_client):
        """An archived room must not accept new messages — it's hidden from the
        UI and shouldn't keep spawning tasks / churning its channel memory."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await chat_client.patch(
            f"/istota/api/chat/rooms/{room['id']}",
            json={"archived": True}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "anyone there?"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 409

    async def test_command_runs_inline_no_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!help"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert "inline_result" in body
        assert "!help" in body["inline_result"]

    async def test_model_prefix_creates_task_with_override(self, chat_client):
        """`!model <alias> <prompt>` must create a real task carrying the model
        override — it's a prefix, not a command (this was broken before)."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!model opus summarize my day"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is not None
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, body["task_id"])
        assert task.model  # canonical Opus id
        assert task.prompt == "summarize my day"  # prefix stripped

    async def test_model_prefix_unknown_alias_returns_usage_inline(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!model bogus do something"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert "Aliases" in body["inline_result"]

    async def test_send_attaches_uploaded_file_to_task(self, chat_client):
        """An uploaded attachment's path must land on the task's attachments
        column so the brain actually sees the file."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        up = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        path = up.json()["path"]
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "summarize this", "attachments": [path]},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, resp.json()["task_id"])
        assert task.attachments == [path]

    async def test_send_drops_foreign_attachment_path(self, chat_client):
        """A path outside the user's web-chat upload root is rejected — a client
        can't get the brain to read arbitrary host paths."""
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "read this", "attachments": ["/etc/passwd"]},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400


@_needs_web_deps
class TestChatDeleteApi:
    async def _create_room(self, client, cookies, name):
        return (await client.post(
            "/istota/api/chat/rooms", json={"name": name}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()

    async def test_delete_room_ok(self, chat_client):
        cookies = await _login(chat_client, "alice")
        # Establish the default `general` room so deleting `scratch` leaves a
        # room behind (deleting the only room just gets it auto-recreated).
        await chat_client.get("/istota/api/chat/rooms", cookies=cookies)
        room = await self._create_room(chat_client, cookies, "scratch")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        rooms = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"]
        assert room["id"] not in [r["id"] for r in rooms]

    async def test_delete_room_with_active_task_409(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = (await chat_client.get(
            "/istota/api/chat/rooms", cookies=cookies,
        )).json()["rooms"][0]
        # Sending a message creates a pending (non-terminal) task.
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "do a thing"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 409
        assert "progress" in resp.json()["error"]

    async def test_cannot_delete_other_users_room(self, chat_client):
        alice = await _login(chat_client, "alice")
        room = await self._create_room(chat_client, alice, "secret")
        bob = await _login(chat_client, "bob")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=bob,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_delete_unknown_room_404(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.delete(
            "/istota/api/chat/rooms/99999", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_delete_requires_csrf(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._create_room(chat_client, cookies, "scratch")
        resp = await chat_client.delete(
            f"/istota/api/chat/rooms/{room['id']}", cookies=cookies,
        )
        assert resp.status_code == 403


@_needs_web_deps
class TestChatTaskActions:
    async def _seed_task(self, username, status="running"):
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            room = db.ensure_default_web_chat_room(c, username)
            tid = db.create_task(
                c, prompt="do a thing", user_id=username, source_type="web",
                conversation_token=room.token, output_target="web",
            )
            db.update_task_status(c, tid, status)
        return tid

    async def test_confirm_marks_pending_and_clears_events(self, chat_client):
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            c.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) VALUES (?,1,'confirmation','{}')",
                (tid,),
            )
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        with db.get_db(mod._config.db_path) as c:
            assert db.get_task(c, tid).status == "pending"
            assert db.get_task_events(c, tid) == []

    async def test_cancel_pending_confirmation_cancels(self, chat_client):
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/cancel", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            assert db.get_task(c, tid).status == "cancelled"

    async def test_cancel_running_sets_flag(self, chat_client):
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="running")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/cancel", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            flag = c.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0]
            assert flag == 1

    async def test_cannot_confirm_other_users_task(self, chat_client):
        await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="pending_confirmation")
        cookies = await _login(chat_client, "bob")
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 403

    async def test_confirm_on_running_task_preserves_events(self, chat_client):
        """Confirming a task that is NOT pending_confirmation must be a no-op —
        it must never wipe a live task's event log."""
        cookies = await _login(chat_client, "alice")
        tid = await self._seed_task("alice", status="running")
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            c.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) VALUES (?,1,'tool_start','{}')",
                (tid,),
            )
        resp = await chat_client.post(
            f"/istota/api/chat/tasks/{tid}/confirm", cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        with db.get_db(mod._config.db_path) as c:
            # Status untouched and the running task's events are intact.
            assert db.get_task(c, tid).status == "running"
            assert len(db.get_task_events(c, tid)) == 1


@_needs_web_deps
class TestChatAttachments:
    async def test_upload_saves_file(self, chat_client):
        import os
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "note.txt"
        assert body["size"] == 11
        assert os.path.exists(body["path"])
        assert "inbox/web-chat" in body["path"].replace(os.sep, "/")

    async def test_disallowed_extension_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_oversize_rejected(self, chat_client):
        import istota.web_app as mod
        mod._config.web.chat.max_attachment_mb = 0  # everything is too big
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/attachments",
            files={"file": ("a.txt", b"x", "text/plain")},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 413
        mod._config.web.chat.max_attachment_mb = 25
