"""Per-room model / effort default.

A room can carry a standing model + effort that applies to every message in it,
regardless of surface (Talk or web) — because it lives on the shared `rooms`
registry, resolved at the single `record_inbound` choke point. An inline
`!model` prefix on a message overrides it. Settable via the `!room` command
(both surfaces) and, later, the web room-settings UI.
"""

import pytest

from istota import db
from istota.commands import cmd_room
from istota.config import (
    Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig,
)
from istota.transport.ingest import record_inbound


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    cfg = Config()
    cfg.db_path = db_path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.talk = TalkConfig(enabled=True, bot_username="istota")
    cfg.nextcloud = NextcloudConfig(url="https://nc.test", username="istota", app_password="p")
    cfg.scheduler = SchedulerConfig()
    cfg.users = {"alice": UserConfig()}
    return cfg


def _ctx(config, conn, args, user_id="alice", token="room1", surface="talk"):
    from istota.commands import CommandContext
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface=surface,
    )


# =============================================================================
# DB layer — rooms.model / rooms.effort columns + setters
# =============================================================================

class TestRoomDefaultsStorage:
    def test_new_room_has_no_defaults(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            room = db.get_room(conn, "room1")
        assert room.model is None
        assert room.effort is None

    def test_set_room_model_effort_roundtrip(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
            room = db.get_room(conn, "room1")
        assert room.model == "claude-opus-4-8"
        assert room.effort == "high"

    def test_set_room_model_effort_clears_with_none(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
            db.set_room_model_effort(conn, "room1", None, None)
            room = db.get_room(conn, "room1")
        assert room.model is None
        assert room.effort is None

    def test_set_room_effort_only(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-sonnet-4-6", None)
            db.set_room_effort(conn, "room1", "medium")
            room = db.get_room(conn, "room1")
        assert room.model == "claude-sonnet-4-6"
        assert room.effort == "medium"


# =============================================================================
# record_inbound — the cross-surface resolution point
# =============================================================================

class TestRecordInboundRoomDefault:
    def test_room_default_applied_when_no_override(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.model == "claude-opus-4-8"
        assert task.effort == "high"

    def test_room_default_applies_across_talk_surface(self, config, db_path):
        """The default lives on the shared registry, so a Talk message in the
        same room resolves it too — the whole point."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tk123", "alice", origin="talk")
            db.add_room_binding(conn, "tk123", "talk", "tk123")
            db.set_room_model_effort(conn, "tk123", "claude-opus-4-8", "high")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="tk123",
                user_id="alice", text="hi", channel_name="#room",
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.model == "claude-opus-4-8"
        assert task.effort == "high"

    def test_inline_override_wins(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
                model="claude-sonnet-4-6", effort=None,
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        # Inline model present → room default (incl. its effort) does not bleed in.
        assert task.model == "claude-sonnet-4-6"
        assert task.effort is None

    def test_explicit_default_escapes_room_default(self, config, db_path):
        """`!model default` resolves to no override (model=None) but must still
        beat the room default — the caller signals this via apply_room_default."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
                model=None, effort=None, apply_room_default=False,
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert (task.model or "") == ""
        assert (task.effort or "") == ""

    def test_ingest_message_prefix_flag_suppresses_default(self, config, db_path):
        """The Talk path (ingest_message) maps model_prefix_used → suppression."""
        from istota.transport import IncomingMessage, ingest_message
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tk1", "alice", origin="talk")
            db.add_room_binding(conn, "tk1", "talk", "tk1")
            db.set_room_model_effort(conn, "tk1", "claude-opus-4-8", "high")
        with db.get_db(db_path) as conn:
            tid = ingest_message(conn, config, IncomingMessage(
                user_id="alice", text="hi", source_type="talk", surface="talk",
                channel_token="tk1", channel_name="#room",
                model=None, effort=None, model_prefix_used=True,
            ))
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, tid)
        assert (task.model or "") == ""
        # And without the flag, the same message inherits the default.
        with db.get_db(db_path) as conn:
            tid2 = ingest_message(conn, config, IncomingMessage(
                user_id="alice", text="hi again", source_type="talk", surface="talk",
                channel_token="tk1", channel_name="#room",
                platform_message_id=999,
            ))
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, tid2).model == "claude-opus-4-8"

    def test_no_default_no_model(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert (task.model or "") == ""
        assert (task.effort or "") == ""


# =============================================================================
# !room command — both surfaces
# =============================================================================

@pytest.mark.asyncio
class TestRoomCommand:
    async def test_show_empty(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            out = await cmd_room(_ctx(config, conn, ""))
        assert "default" in out.lower()

    async def test_set_model(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            out = await cmd_room(_ctx(config, conn, "model opus"))
            room = db.get_room(conn, "room1")
        assert room.model == "claude-opus-4-8"
        assert "opus" in out.lower()

    async def test_set_model_with_effort_alias(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            await cmd_room(_ctx(config, conn, "model opus-high"))
            room = db.get_room(conn, "room1")
        assert room.model == "claude-opus-4-8"
        assert room.effort == "high"

    async def test_model_alias_preserves_separate_effort(self, config, db_path):
        """`!room effort` then `!room model <plain alias>` keeps the effort — the
        two knobs are orthogonal (a plain alias carries no effort of its own)."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            await cmd_room(_ctx(config, conn, "effort high"))
            await cmd_room(_ctx(config, conn, "model opus"))
            room = db.get_room(conn, "room1")
        assert room.model == "claude-opus-4-8"
        assert room.effort == "high"

    async def test_unknown_alias_shows_usage(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            out = await cmd_room(_ctx(config, conn, "model bogus"))
            room = db.get_room(conn, "room1")
        assert room.model is None
        assert "alias" in out.lower() or "usage" in out.lower()

    async def test_model_default_clears(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
            out = await cmd_room(_ctx(config, conn, "model default"))
            room = db.get_room(conn, "room1")
        assert room.model is None
        assert room.effort is None
        assert "default" in out.lower()

    async def test_set_effort(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", None)
            await cmd_room(_ctx(config, conn, "effort medium"))
            room = db.get_room(conn, "room1")
        assert room.effort == "medium"

    async def test_set_effort_invalid(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            out = await cmd_room(_ctx(config, conn, "effort turbo"))
            room = db.get_room(conn, "room1")
        assert room.effort is None
        assert "low" in out.lower()  # usage lists valid levels

    async def test_effort_default_clears(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
            await cmd_room(_ctx(config, conn, "effort default"))
            room = db.get_room(conn, "room1")
        assert room.effort is None

    async def test_works_on_web_surface(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            out = await cmd_room(
                _ctx(config, conn, "model sonnet", surface="web")
            )
            room = db.get_room(conn, "room1")
        assert room.model == "claude-sonnet-4-6"
        assert isinstance(out, str)
