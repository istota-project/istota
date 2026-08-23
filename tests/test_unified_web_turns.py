"""Stage 4 — web turns into the canonical messages store.

Covers: idempotent assistant-turn storage, the system-message lane the
WebTransport delivery path now writes, and the web display loader rendering
cross-surface turns + canonical system notifications.
"""

import pytest

from istota import db
from istota.config import Config

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


def _add_task(conn, token, prompt, result, *, source_type="web", status="completed"):
    row = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status) VALUES (?, 'u', ?, ?, ?, ?) RETURNING id",
        (source_type, token, prompt, result, status),
    ).fetchone()
    return int(row["id"])


class TestStoreTurnMessage:
    def test_stores_assistant_and_is_idempotent(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        mid = db.store_turn_message(
            conn, "r", role="assistant", body="answer", task_id=5, origin_surface="web",
        )
        assert mid is not None
        # second call (e.g. a retry re-completing) is a no-op
        assert db.store_turn_message(
            conn, "r", role="assistant", body="answer", task_id=5, origin_surface="web",
        ) is None
        msgs = db.get_messages(conn, "r")
        assert [(m.role, m.body) for m in msgs] == [("assistant", "answer")]

    def test_completed_task_becomes_caught_up(self, conn):
        # The scheduler stores user (Stage 3) + assistant (Stage 4); once the
        # assistant row lands, get_conversation_history serves from messages.
        db.register_room(conn, "r", "u", origin="web")
        t = _add_task(conn, "r", "q", "a")
        db.store_turn_message(conn, "r", role="user", body="q", task_id=t, origin_surface="web")
        db.store_turn_message(conn, "r", role="assistant", body="a", task_id=t, origin_surface="web")
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [(m.prompt, m.result) for m in hist] == [("q", "a")]


class TestSystemMessages:
    def test_list_system_messages_only(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        db.add_message(conn, "r", role="user", body="hi", origin_surface="web", task_id=1)
        db.add_message(conn, "r", role="system", body="disk full", origin_surface="web", title="Alert")
        sysmsgs = db.list_system_messages(conn, "r")
        assert [(m.role, m.title, m.body) for m in sysmsgs] == [("system", "Alert", "disk full")]


class TestWebDeliveryWritesCanonicalStore:
    def test_append_blocking_writes_messages_system_row(self, db_path):
        from istota.transport.web import _append_blocking
        cfg = Config()
        cfg.db_path = db_path
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "u", "Chat")
            token = room.token
        mid = _append_blocking(cfg, token, "alert body", "Heads up")
        assert mid is not None
        with db.get_db(db_path) as conn:
            sysmsgs = db.list_system_messages(conn, token)
        assert [(m.title, m.body) for m in sysmsgs] == [("Heads up", "alert body")]

    def test_append_blocking_drops_unknown_room(self, db_path):
        from istota.transport.web import _append_blocking
        cfg = Config()
        cfg.db_path = db_path
        assert _append_blocking(cfg, "no-such-token", "x", None) is None


@_needs_web_deps
class TestDisplayLoaderCrossSurface:
    def _loader(self, db_path):
        from istota import web_app
        web_app._config = Config()
        web_app._config.db_path = db_path
        return web_app._chat_room_messages

    def test_talk_and_web_turns_both_render(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "u", origin="talk")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
            _add_task(conn, "cpz", "talk q", "talk a", source_type="talk")
            _add_task(conn, "cpz", "web q", "web a", source_type="web")
        loader = self._loader(db_path)
        out = loader("u", "cpz", 50)
        texts = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("user", "talk q") in texts
        assert ("assistant", "talk a") in texts
        assert ("user", "web q") in texts
        assert ("assistant", "web a") in texts

    def test_rename_propagates_to_registry(self, db_path):
        from istota import web_app
        web_app._config = Config()
        web_app._config.db_path = db_path
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "u", "Old Name")
        web_app._chat_update_room("u", room.id, name="New Name", archived=None)
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token).name == "New Name"

    def test_archive_propagates_to_registry(self, db_path):
        from istota import web_app
        web_app._config = Config()
        web_app._config.db_path = db_path
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "u", "Chat")
        web_app._chat_update_room("u", room.id, name=None, archived=True)
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token).archived is True
            assert [r.token for r in db.list_rooms(conn, "u")] == []  # hidden

    def test_inflight_talk_task_surfaces_as_active(self, db_path):
        # Stage 7 substrate: a running Talk-originated task in the room shows up
        # as an active_task so the open web client can stream its progress live.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "u", origin="talk")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
            running = _add_task(conn, "cpz", "talk q", None,
                                source_type="talk", status="running")
            db.add_message(conn, "cpz", role="user", body="talk q",
                           origin_surface="talk", task_id=running)
        out = self._loader(db_path)("u", "cpz", 50)
        active_ids = [a["id"] for a in out["active_tasks"]]
        assert running in active_ids
        # the user's Talk prompt is in the transcript for the client to show
        assert ("user", "talk q") in [(m["role"], m["text"]) for m in out["messages"]]

    def test_system_notifications_from_messages(self, db_path):
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "u", "Chat")
            token = room.token
            _add_task(conn, token, "q", "a", source_type="web")
            db.add_message(conn, token, role="system", body="logged out", origin_surface="web", title="Note")
        loader = self._loader(db_path)
        out = loader("u", token, 50)
        sys_msgs = [m for m in out["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1
        assert "logged out" in sys_msgs[0]["text"]
        assert sys_msgs[0]["notif_id"] is not None

    def test_a_mirrored_alert_shows_its_title_once(self, db_path, tmp_path):
        """The reported symptom of ISSUE-311, through the seam that produced it.

        This route composes a system message as the bolded title, a blank line,
        then the body. `notification_store._delivery_text` had already composed
        the *same* title into the front of the text Talk was sent, and the mirror
        stored that text verbatim, so an alert rendered here with its label
        twice. `_dispatch` now hands the mirror the body and the title
        separately, which is the arrangement this route was written for.
        """
        from istota.config import Config, NextcloudConfig, UserConfig
        from istota import notifications
        from unittest.mock import AsyncMock, patch

        title = "Security alert — task #7"
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"u": UserConfig()},
        )
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "u", "Chat")
            token = room.token
            # The mirror writes against the Talk-side token, so register one.
            db.register_room(conn, "talkroom7", "u", origin="talk")
            conn.commit()

        with patch.object(notifications, "_send_talk",
                          new=AsyncMock(return_value=11)):
            notifications.send_notification(
                config, "u", f"{title}\n\nSomebody tried something.",
                surface="talk:talkroom7", title=title,
            )

        out = self._loader(db_path)("u", "talkroom7", 50)
        sys_msgs = [m for m in out["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1
        text = sys_msgs[0]["text"]
        assert text.count(title) == 1, text
        assert "Somebody tried something." in text
        assert token  # the web room exists; the alert landed in the Talk one
