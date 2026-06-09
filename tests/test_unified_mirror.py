"""Stage 5 — mirror fan-out via output_target="room".

`room` expands at resolve time by the room's live bindings (not a static alias):
the origin delivery plus a push mirror to every non-origin *push* binding.
Stream bindings (web) are skipped — their clients read the shared canonical
store, so a push would double-post.
"""

import pytest

from istota import db
from istota.config import Config
from istota.transport.routing import (
    Destination,
    parse_output_target,
    resolve_delivery_plan,
)


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    return cfg


def _task(**kwargs):
    defaults = dict(
        id=1, status="pending", source_type="web", user_id="alice",
        prompt="x", conversation_token=None, priority=5,
        attempt_count=0, max_attempts=3,
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


class TestParseRoom:
    def test_room_parses_to_single_destination(self):
        assert parse_output_target("room") == [Destination("room")]


class TestRoomExpansion:
    def test_web_origin_mirrors_to_bound_talk(self, config):
        # web-origin room bound to talk: origin stream + talk push mirror.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-1", "web", "web-alice-1")
            db.add_room_binding(conn, "web-alice-1", "talk", "talktok9")
        task = _task(source_type="web", conversation_token="web-alice-1",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        surfaces = {(d.surface, d.channel, d.kind) for d in plan}
        assert ("web", "stream", "stream") in surfaces
        assert ("talk", "talktok9", "push") in surfaces
        talk_dest = next(d for d in plan if d.surface == "talk")
        assert talk_dest.mirror is True

    def test_web_only_room_mirrors_nowhere(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-2", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-2", "web", "web-alice-2")
        task = _task(source_type="web", conversation_token="web-alice-2",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["web"]
        assert plan[0].kind == "stream"

    def test_talk_origin_does_not_push_to_web_binding(self, config):
        # talk-origin room bound to web: web binding is a stream surface, so no
        # mirror push — the web view renders Talk turns from the shared store.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
            db.add_room_binding(conn, "cpz", "web", "cpz")
        task = _task(source_type="talk", conversation_token="cpz",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["talk"]
        assert plan[0].channel == "cpz"

    def test_room_expands_by_live_bindings_not_static_alias(self, config):
        # Same task, before vs after adding a talk binding: the plan changes.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-3", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-3", "web", "web-alice-3")
        task = _task(source_type="web", conversation_token="web-alice-3",
                     output_target="room")
        before = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in before] == ["web"]
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, "web-alice-3", "talk", "newtalk")
        after = resolve_delivery_plan(config, task, None)
        assert {d.surface for d in after} == {"web", "talk"}


class TestExternalIdLedger:
    def test_set_and_detect_external_id(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "r", "alice", origin="web")
            mid = db.add_message(
                conn, "r", role="assistant", body="bot reply",
                origin_surface="web", task_id=1,
            )
            db.set_message_external_id(conn, mid, "talk", "8888")
            assert db.message_has_external_id(conn, "r", "talk", "8888") is True
            assert db.message_has_external_id(conn, "r", "talk", "9999") is False
