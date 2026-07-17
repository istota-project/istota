"""ISSUE-164 — a foreign task's conversational reply fanned into the web room
it is conversing in must render as an assistant chat bubble, not as a
``role='system'`` cmd-output note.

An email round-trip continued from a web room (``source_type="email"``,
``output_target="web:<own token>,email"``) used to deliver its web leg via
``WebTransport.deliver`` → a ``role='system'`` row → the cmd-output block. The
fix stores it as a ``role='assistant'`` spine row (``scheduler._store_web_room_turn``)
so the transcript renders it as a normal bubble, and drops the own-conversation
web dest from the system-push lane. Foreign-room pushes stay ``role='system'``.
"""

from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config
from istota.scheduler import _store_web_room_turn

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


def _email_task(token, task_id=20):
    return SimpleNamespace(
        source_type="email", conversation_token=token, id=task_id,
    )


class TestStoreWebRoomTurn:
    def test_stores_assistant_turn_with_web_origin(self, conn):
        db.register_room(conn, "cpzpcfx2", "u", origin="web")
        _store_web_room_turn(conn, _email_task("cpzpcfx2"), "Reply's queued to go back")
        msgs = db.get_messages(conn, "cpzpcfx2")
        assert [(m.role, m.body, m.origin_surface) for m in msgs] == [
            ("assistant", "Reply's queued to go back", "web"),
        ]
        # It must NOT be a system (cmd-output) row.
        assert db.list_system_messages(conn, "cpzpcfx2") == []

    def test_noop_when_room_not_registered(self, conn):
        # Room token with no registry row → deleted room, nothing to render into.
        _store_web_room_turn(conn, _email_task("ghosttoken"), "reply")
        assert db.get_messages(conn, "ghosttoken") == []

    def test_noop_when_no_conversation_token(self, conn):
        _store_web_room_turn(conn, _email_task(None), "x")  # nothing raised/stored

    def test_idempotent_across_retries(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        task = _email_task("r", task_id=42)
        _store_web_room_turn(conn, task, "reply")
        _store_web_room_turn(conn, task, "reply")  # retry re-completes
        assert len(db.get_messages(conn, "r")) == 1

    def test_does_not_double_store_when_native_assistant_row_exists(self, conn):
        # A talk/web-origin task is already stored by the success branch with the
        # native origin; the later web-room store dedups on (room, role, task_id).
        db.register_room(conn, "r", "u", origin="talk")
        db.store_turn_message(
            conn, "r", role="assistant", body="answer", task_id=7,
            origin_surface="talk",
        )
        _store_web_room_turn(
            conn, SimpleNamespace(source_type="talk", conversation_token="r", id=7),
            "answer",
        )
        msgs = db.get_messages(conn, "r")
        assert len(msgs) == 1
        assert msgs[0].origin_surface == "talk"  # native origin wins


@_needs_web_deps
class TestWebReaderRendersReplyAsBubble:
    def test_reply_renders_as_assistant_bubble_not_system(self, db_path):
        from istota import web_app

        web_app._config = Config()
        web_app._config.db_path = db_path
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "u", origin="web")
            _store_web_room_turn(
                conn, _email_task("cpzpcfx2"), "Reply's queued to go back",
            )
        out = web_app._chat_room_messages("u", "cpzpcfx2", 50)
        rendered = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("assistant", "Reply's queued to go back") in rendered
        assert all(r != "system" for r, _ in rendered)
