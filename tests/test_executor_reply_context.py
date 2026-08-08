"""Prompt construction for a reply that cites a canonical `messages.id`.

Two mechanisms, doing different jobs: the request-section quote frame (always
present, from the stored snapshot) and the force-included parent turn (when the
parent resolves to a completed task). The regression guard that matters most is
the namespace one — a canonical id must never resolve through the Talk-native
column, even when the two collide numerically in the same room.
"""

import pytest

from istota import db
from istota.config import Config, UserConfig


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        db.register_room(c, "room1", "alice", origin="web", name="Room")
        yield c


def _config(tmp_path, db_path):
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


def _completed(conn, prompt, result, *, token="room1", **kwargs):
    tid = db.create_task(
        conn, prompt=prompt, user_id="alice", source_type="web",
        conversation_token=token, **kwargs,
    )
    db.update_task_status(conn, tid, "completed", result=result)
    return tid


class TestForceInclude:
    def test_canonical_parent_is_force_included(self, tmp_path, conn):
        from istota.executor import _ensure_reply_parent_in_history

        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        config = _config(tmp_path, db_path)
        parent = _completed(conn, "the long question", "the long answer")
        msg_id = db.add_message(
            conn, "room1", role="assistant", body="the long answer",
            origin_surface="web", task_id=parent,
        )

        task = db.Task(
            id=parent + 500, user_id="alice", prompt="no, the second one",
            source_type="web", conversation_token="room1", status="running",
            reply_to_message_id=msg_id,
        )
        history, reply_parent = _ensure_reply_parent_in_history(
            task, [], config, conn,
        )
        assert reply_parent is not None
        assert reply_parent.id == parent
        # Prepended, and the whole turn — not the 1000-char snapshot.
        assert history[0].result == "the long answer"
        assert history[0].prompt == "the long question"

    def test_canonical_id_never_resolves_through_the_talk_column(
        self, tmp_path, conn,
    ):
        """The namespace hazard this design exists to avoid: in a Talk-bound
        room, `messages.id` and Talk message ids are both small integers. A
        canonical citation must not be able to surface an unrelated Talk turn."""
        from istota.executor import _ensure_reply_parent_in_history

        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        config = _config(tmp_path, db_path)
        # The real parent, addressed canonically.
        parent = _completed(conn, "web question", "web answer")
        msg_id = db.add_message(
            conn, "room1", role="assistant", body="web answer",
            origin_surface="web", task_id=parent,
        )
        # An unrelated Talk turn in the same room whose *Talk* id happens to
        # equal that canonical id.
        decoy = _completed(
            conn, "unrelated talk question", "unrelated talk answer",
            talk_message_id=msg_id,
        )

        task = db.Task(
            id=decoy + 500, user_id="alice", prompt="yes, that one",
            source_type="web", conversation_token="room1", status="running",
            reply_to_message_id=msg_id,
        )
        _history, reply_parent = _ensure_reply_parent_in_history(
            task, [], config, conn,
        )
        assert reply_parent is not None
        assert reply_parent.id == parent, "resolved through the Talk namespace"

    def test_retention_deleted_parent_degrades_to_the_snapshot(
        self, tmp_path, conn,
    ):
        from istota.executor import _ensure_reply_parent_in_history

        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        config = _config(tmp_path, db_path)
        # A message row whose task is gone (retention), which is exactly the
        # state `messages` is designed to outlive.
        msg_id = db.add_message(
            conn, "room1", role="assistant", body="an answer",
            origin_surface="web", task_id=99999,
        )

        task = db.Task(
            id=4242, user_id="alice", prompt="follow up", source_type="web",
            conversation_token="room1", status="running",
            reply_to_message_id=msg_id, reply_to_content="an answer",
        )
        history, reply_parent = _ensure_reply_parent_in_history(
            task, [], config, conn,
        )
        assert reply_parent is not None
        assert reply_parent.result == "an answer"
        assert history[0] is reply_parent

    def test_talk_reply_still_resolves_through_its_own_column(
        self, tmp_path, conn,
    ):
        from istota.executor import _ensure_reply_parent_in_history

        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        config = _config(tmp_path, db_path)
        parent = _completed(
            conn, "talk question", "talk answer", talk_message_id=77,
        )

        task = db.Task(
            id=parent + 500, user_id="alice", prompt="follow up",
            source_type="talk", conversation_token="room1", status="running",
            reply_to_talk_id=77,
        )
        _history, reply_parent = _ensure_reply_parent_in_history(
            task, [], config, conn,
        )
        assert reply_parent is not None
        assert reply_parent.id == parent

    def test_no_citation_is_a_no_op(self, tmp_path, conn):
        from istota.executor import _ensure_reply_parent_in_history

        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        config = _config(tmp_path, db_path)
        task = db.Task(
            id=1, user_id="alice", prompt="hello", source_type="web",
            conversation_token="room1", status="running",
        )
        history, reply_parent = _ensure_reply_parent_in_history(
            task, [], config, conn,
        )
        assert history == []
        assert reply_parent is None


class TestQuoteFrame:
    def _prompt_for(self, tmp_path, task):
        from istota.executor import build_prompt

        config = _config(tmp_path, tmp_path / "istota.db")
        return build_prompt(task, [], config)

    def test_request_section_carries_the_quote(self, tmp_path):
        task = db.Task(
            id=1, user_id="alice", prompt="no, the second one",
            source_type="web", conversation_token="room1", status="running",
            reply_to_message_id=7,
            reply_to_content="First option is X.\nSecond option is Y.",
        )
        prompt = self._prompt_for(tmp_path, task)
        assert "## User's request" in prompt
        request = prompt.split("## User's request", 1)[1]
        assert "> Replying to:" in request
        # Verbatim, blockquoted line by line.
        assert "> First option is X." in request
        assert "> Second option is Y." in request
        # The user's own text follows the quote.
        assert request.index("> Second option is Y.") < request.index(
            "no, the second one",
        )

    def test_frame_is_absent_without_a_citation(self, tmp_path):
        task = db.Task(
            id=1, user_id="alice", prompt="hello", source_type="web",
            conversation_token="room1", status="running",
        )
        assert "Replying to:" not in self._prompt_for(tmp_path, task)

    def test_talk_reply_also_gets_the_frame(self, tmp_path):
        """The frame is keyed on the snapshot, not on the surface — a Talk
        reply's `(In reply to: …)` fallback only appeared when history was
        empty, so on a busy room the marker vanished when most needed."""
        task = db.Task(
            id=1, user_id="alice", prompt="yes", source_type="talk",
            conversation_token="room1", status="running",
            reply_to_talk_id=77, reply_to_content="Shall I proceed?",
        )
        prompt = self._prompt_for(tmp_path, task)
        assert "> Shall I proceed?" in prompt.split("## User's request", 1)[1]
