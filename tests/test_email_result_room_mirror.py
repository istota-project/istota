"""An email task's Talk-delivered result reaches the web view of the room it
landed in.

`scheduler._store_room_turn` keys on `task.conversation_token`; the Talk post
keys on the resolved `_talk_target_for_delivery`. For an email task those are two
different things — a synthetic thread hash naming no room, and the user's DM room
via the resolve ladder — so Talk showed the reply and the web view of that same
room showed nothing. ISSUE-242 closed the identical gap on the *notification*
side only, which is what made the divergence visible: the alert mirrored, the
answer under it did not.

The mirror is a `role='system'` row, not an assistant one, because the email's
user turn is deliberately not in that room (`record_inbound`'s `mirror_only` gate
is room existence) — an assistant row would be a bubble answering a question the
room does not hold.
"""

from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config
from istota.scheduler import _talk_result_mirror_body


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def conn(config):
    with db.get_db(config.db_path) as c:
        yield c


def _task(token, source_type="email", task_id=7):
    return SimpleNamespace(
        id=task_id, source_type=source_type, conversation_token=token,
        user_id="alice",
    )


def _dest(surface, channel):
    return SimpleNamespace(surface=surface, channel=channel)


class TestMirrorBodyDecision:
    def test_email_thread_token_mirrors_to_delivered_room(self, conn):
        # The reported case: the task's own token is the synthetic thread hash,
        # the post went to the user's DM room.
        db.register_room(conn, "dmtoken1", "alice", origin="talk")
        body = _talk_result_mirror_body(
            conn, _task("a1b2c3d4e5f60718"), "dmtoken1", "the reply", [],
        )
        assert body == "the reply"

    def test_no_mirror_when_task_room_is_web_visible(self, conn):
        # `_store_room_turn` already wrote an assistant row here; a system row
        # beside it would show the answer twice.
        db.register_room(conn, "roomtok", "alice", origin="talk")
        assert _talk_result_mirror_body(
            conn, _task("roomtok", source_type="talk"), "roomtok", "hi", [],
        ) is None

    def test_no_mirror_when_plan_already_pushes_web_to_that_room(self, conn):
        # A route naming both legs of one room must produce one row. The Talk
        # ref and the canonical room token differ on a promoted room, so the
        # comparison has to resolve the binding rather than compare raw tokens.
        db.register_room(conn, "web-alice-1", "alice", origin="web")
        db.add_room_binding(conn, "web-alice-1", "talk", "talktok9")
        assert _talk_result_mirror_body(
            conn, _task("a1b2c3d4e5f60718"), "talktok9", "hi",
            [_dest("web", "web-alice-1")],
        ) is None

    def test_mirrors_when_web_push_targets_a_different_room(self, conn):
        db.register_room(conn, "dmtoken1", "alice", origin="talk")
        assert _talk_result_mirror_body(
            conn, _task("a1b2c3d4e5f60718"), "dmtoken1", "hi",
            [_dest("web", "web-alice-other")],
        ) == "hi"

    def test_task_with_no_token_still_mirrors(self, conn):
        db.register_room(conn, "dmtoken1", "alice", origin="talk")
        assert _talk_result_mirror_body(
            conn, _task(None), "dmtoken1", "hi", [],
        ) == "hi"


class TestMirrorWrite:
    def test_writes_a_system_row_carrying_the_talk_id(self, config, conn):
        from istota.notifications import mirror_talk_to_room

        db.register_room(conn, "dmtoken1", "alice", origin="talk")
        conn.commit()
        mirror_talk_to_room(config, "dmtoken1", "the reply", talk_message_id=414)
        with db.get_db(config.db_path) as c:
            msgs = db.get_messages(c, "dmtoken1")
        assert [(m.role, m.body) for m in msgs] == [("system", "the reply")]
        # The stamp is what lets a cited reply walk back to the Talk post, and
        # what caps the Talk→web read-sync cursor.
        assert msgs[0].external_ids == {"talk": "414"}

    def test_unregistered_room_is_a_noop(self, config):
        from istota.notifications import mirror_talk_to_room

        mirror_talk_to_room(config, "ghosttoken", "x", talk_message_id=1)
        with db.get_db(config.db_path) as c:
            assert db.get_messages(c, "ghosttoken") == []
