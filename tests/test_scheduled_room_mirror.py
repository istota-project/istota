"""ISSUE-133 — scheduled (cron) job posts to a Talk room mirror into the
canonical messages store so the room's web view renders them.

The web transcript reader renders assistant rows of a registered room; this
covers the producer half. The scheduled-specific ``_store_scheduled_room_turn``
was subsumed by the general ``scheduler._store_room_turn`` (canonical room
transcript for all source types), which writes the row for a task of any source
type whose conversation token names a registered room — so scheduled parity is
preserved via the general helper.
"""

from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config
from istota.scheduler import _store_room_turn

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _scheduled_task(token, task_id=10):
    return SimpleNamespace(
        source_type="scheduled", conversation_token=token, id=task_id,
    )


class TestStoreScheduledRoomTurn:
    def test_stores_assistant_turn_with_scheduled_origin(self, conn):
        db.register_room(conn, "2ay6qic9", "u", origin="talk")
        _store_room_turn(conn, _scheduled_task("2ay6qic9"), "you are now at home")
        msgs = db.get_messages(conn, "2ay6qic9")
        assert [(m.role, m.body, m.origin_surface) for m in msgs] == [
            ("assistant", "you are now at home", "scheduled"),
        ]

    def test_noop_when_room_not_registered(self, conn):
        # A Talk room the bot only ever posts alerts to (no human turn) has no
        # registry row → not web-visible → nothing to mirror, no orphan row.
        _store_room_turn(conn, _scheduled_task("ghosttoken"), "alert")
        assert db.get_messages(conn, "ghosttoken") == []

    def test_noop_when_no_conversation_token(self, conn):
        _store_room_turn(conn, _scheduled_task(None), "x")
        # nothing raised, nothing stored anywhere

    def test_idempotent_across_retries(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        task = _scheduled_task("r", task_id=42)
        _store_room_turn(conn, task, "alert")
        _store_room_turn(conn, task, "alert")  # retry re-completes
        msgs = db.get_messages(conn, "r")
        assert len(msgs) == 1


@_needs_web_deps
class TestWebReaderRendersScheduledPost:
    def test_scheduled_post_renders_assistant_only(self, db_path):
        from istota import web_app

        web_app._config = Config()
        web_app._config.db_path = db_path
        with db.get_db(db_path) as conn:
            db.register_room(conn, "2ay6qic9", "u", origin="talk")
            db.add_room_binding(conn, "2ay6qic9", "talk", "2ay6qic9")
            _store_room_turn(
                conn, _scheduled_task("2ay6qic9"), "you are now at home",
            )
        out = web_app._chat_room_messages("u", "2ay6qic9", 50)
        rendered = [(m["role"], m["text"]) for m in out["messages"]]
        # The assistant post renders; the synthetic cron prompt stays hidden.
        assert ("assistant", "you are now at home") in rendered
        assert all(r != "user" for r, _ in rendered)
