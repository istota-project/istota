"""Tests for the web chat delivery surface (WebTransport) — ISSUE-121.

Web chat is now a user-routable delivery surface: alerts, the verbose execution
log, and notifications routed to ``web`` post an unsolicited message into the
user's room. WebTransport.deliver writes a ``web_chat_messages`` row; the
interactive task path is unchanged (still a stream over task_events).
"""

from __future__ import annotations

from istota import db
from istota.async_runtime import run_coro
from istota.config import Config
from istota.transport._types import DeliveryOptions
from istota.transport.web import WebTransport, default_web_room_token


def _config(tmp_path) -> Config:
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config()
    config.db_path = db_path
    return config


def _task(user_id: str, token: str | None = None) -> db.Task:
    return db.Task(
        id=1, status="completed", source_type="scheduled",
        user_id=user_id, prompt="hi", conversation_token=token,
    )


class TestCapabilities:
    def test_user_routable_and_stream(self):
        caps = WebTransport(Config()).capabilities
        assert caps.user_routable is True
        assert caps.surface_class == "stream"
        # Non-edit: the log path delivers one final summary, not a tool stream.
        assert caps.supports_edit is False
        assert caps.supports_progress_ack is False

    def test_construction_does_no_io(self):
        # make_registry builds this with no DB path set; must not touch the DB.
        WebTransport(Config())


class TestDefaultRoomToken:
    def test_provisions_and_returns_general(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        assert token and token.startswith("web-alice-")
        with db.get_db(config.db_path) as conn:
            rooms = db.list_web_chat_rooms(conn, "alice")
        assert [r.name for r in rooms] == ["general"]

    def test_idempotent(self, tmp_path):
        config = _config(tmp_path)
        assert default_web_room_token(config, "alice") == default_web_room_token(
            config, "alice"
        )

    def test_no_db_path_returns_none(self):
        config = Config()
        config.db_path = None
        assert default_web_room_token(config, "alice") is None


class TestDeliver:
    def test_appends_message_to_room(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        transport = WebTransport(config)

        msg_id = run_coro(transport.deliver(token, "heads up"))
        assert isinstance(msg_id, int)

        with db.get_db(config.db_path) as conn:
            msgs = db.list_web_chat_messages(conn, token)
        assert len(msgs) == 1
        assert msgs[0].text == "heads up"
        assert msgs[0].role == "system"
        assert msgs[0].user_id == "alice"

    def test_carries_title_from_options(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        run_coro(WebTransport(config).deliver(
            token, "body", options=DeliveryOptions(title="Alert"),
        ))
        with db.get_db(config.db_path) as conn:
            msgs = db.list_web_chat_messages(conn, token)
        assert msgs[0].title == "Alert"

    def test_owner_derived_from_room_not_caller(self, tmp_path):
        # A token belonging to bob is attributed to bob even if a task for a
        # different user is passed — the room is the source of truth.
        config = _config(tmp_path)
        bob_token = default_web_room_token(config, "bob")
        run_coro(WebTransport(config).deliver(
            bob_token, "x", task=_task("alice"),
        ))
        with db.get_db(config.db_path) as conn:
            msgs = db.list_web_chat_messages(conn, bob_token)
        assert msgs[0].user_id == "bob"

    def test_unknown_token_with_no_task_drops(self, tmp_path):
        config = _config(tmp_path)
        # No room exists for this token and no task → no user to attribute to.
        assert run_coro(WebTransport(config).deliver("web-ghost-000", "x")) is None
        with db.get_db(config.db_path) as conn:
            assert db.list_web_chat_messages(conn, "web-ghost-000") == []

    def test_missing_room_drops_even_with_task(self, tmp_path):
        config = _config(tmp_path)
        # Room tokens are minted at room creation, so a token with no room row is
        # a *deleted* room (never a pending one). Delivery must drop + WARN rather
        # than insert an orphan row that can never render — even with a task
        # present (e.g. an email reply routed to a since-deleted origin room).
        msg_id = run_coro(WebTransport(config).deliver(
            "web-gone-000", "x", task=_task("carol"),
        ))
        assert msg_id is None
        with db.get_db(config.db_path) as conn:
            assert db.list_web_chat_messages(conn, "web-gone-000") == []

    def test_empty_target_with_no_task_returns_none(self, tmp_path):
        config = _config(tmp_path)
        assert run_coro(WebTransport(config).deliver("", "x")) is None


class TestInteractiveResultNotPushed:
    """The critical invariant: a source_type="web" task's own result still
    streams over task_events — registering WebTransport must not make
    resolve_delivery_plan push it via deliver()."""

    def test_web_task_result_resolves_to_stream_only(self, tmp_path):
        from istota.transport import make_registry
        from istota.transport.routing import plan_has_surface, resolve_delivery_plan
        config = _config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target="web",
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        # Stream, not push — nothing for the scheduler to deliver via WebTransport.
        assert all(d.kind == "stream" for d in plan)


class TestResolveTarget:
    def test_resolves_default_room(self, tmp_path):
        config = _config(tmp_path)
        token = WebTransport(config).resolve_target(_task("alice"))
        assert token and token.startswith("web-alice-")


class TestEdit:
    def test_edit_is_noop(self, tmp_path):
        config = _config(tmp_path)
        assert run_coro(WebTransport(config).edit("web-x", 1, "y")) is None
