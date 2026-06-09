"""ISSUE-133 follow-up — scheduled (cron) job posts must not pollute the
canonical messages store with raw NO_ACTION/ACTION text or empty synthetic
prompts.

Covers the shared `scheduled_assistant_body` normalizer, the corrected
transcript backfill, the conversational-only `_messages_caught_up` scoping, and
the one-time cleanup migration for rows the earlier blanket backfill imported.
"""

import sqlite3

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


def _add_task(conn, token, prompt, result, *, source_type="talk",
              status="completed", heartbeat_silent=0):
    row = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status, heartbeat_silent) VALUES (?, 'u', ?, ?, ?, ?, ?) "
        "RETURNING id",
        (source_type, token, prompt, result, status, heartbeat_silent),
    ).fetchone()
    return int(row["id"])


class TestScheduledAssistantBody:
    def test_non_silent_returns_raw(self):
        assert db.scheduled_assistant_body(False, "ACTION: x") == "ACTION: x"
        assert db.scheduled_assistant_body(False, "daily sync done") == "daily sync done"

    def test_silent_action_prefix_stripped(self):
        assert db.scheduled_assistant_body(True, "ACTION: You are now at Home") == "You are now at Home"

    def test_silent_embedded_action_stripped(self):
        assert db.scheduled_assistant_body(True, "thinking…\nACTION: do it") == "do it"

    def test_silent_no_action_omitted(self):
        assert db.scheduled_assistant_body(True, "NO_ACTION:") is None

    def test_silent_no_prefix_posts_as_is(self):
        assert db.scheduled_assistant_body(True, "plain notice") == "plain notice"


class TestBackfillNormalizesScheduled:
    def test_no_action_tick_produces_no_rows(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        _add_task(conn, "r", "loc cron", "NO_ACTION:",
                  source_type="scheduled", heartbeat_silent=1)
        db.backfill_room_messages_from_tasks(conn, "r")
        assert db.get_messages(conn, "r") == []

    def test_action_tick_stores_stripped_assistant_only(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        _add_task(conn, "r", "loc cron", "ACTION: You are now at Home",
                  source_type="scheduled", heartbeat_silent=1)
        db.backfill_room_messages_from_tasks(conn, "r")
        msgs = db.get_messages(conn, "r")
        assert [(m.role, m.body, m.origin_surface) for m in msgs] == [
            ("assistant", "You are now at Home", "scheduled"),
        ]

    def test_non_silent_scheduled_stores_raw_assistant_only(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        _add_task(conn, "r", "money sync", "Synced 3 transactions",
                  source_type="scheduled", heartbeat_silent=0)
        db.backfill_room_messages_from_tasks(conn, "r")
        msgs = db.get_messages(conn, "r")
        assert [(m.role, m.body) for m in msgs] == [("assistant", "Synced 3 transactions")]

    def test_conversational_turn_unchanged(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        _add_task(conn, "r", "hi", "hello", source_type="talk")
        db.backfill_room_messages_from_tasks(conn, "r")
        msgs = db.get_messages(conn, "r")
        assert [(m.role, m.body) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


class TestCaughtUpScoping:
    def test_scheduled_gap_does_not_block_messages_path(self, conn):
        # A room whose conversational turns are all mirrored stays on the
        # messages path even though a scheduled NO_ACTION task has no row.
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1", source_type="talk")
        db.backfill_room_messages_from_tasks(conn, "r")  # mirrors t1, omits cron
        _add_task(conn, "r", "loc cron", "NO_ACTION:",
                  source_type="scheduled", heartbeat_silent=1)
        assert db._messages_caught_up(conn, "r") is True
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [(m.id, m.prompt, m.result) for m in hist] == [(t1, "q1", "a1")]

    def test_missing_conversational_turn_still_falls_back(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1", source_type="talk")
        db.backfill_room_messages_from_tasks(conn, "r")
        t2 = _add_task(conn, "r", "q2", "a2", source_type="talk")  # not mirrored
        assert db._messages_caught_up(conn, "r") is False
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [m.id for m in hist] == [t1, t2]


class TestCleanupMigration:
    def _seed_garbage(self, conn):
        db.register_room(conn, "2ay6qic9", "u", origin="talk")
        # Empty synthetic-prompt user rows + raw scheduled assistant rows, as the
        # old blanket backfill produced them.
        for tid, role, body in (
            (1, "user", ""), (1, "assistant", "ACTION: You are now at Home"),
            (2, "user", ""), (2, "assistant", "NO_ACTION:"),
            (3, "user", ""), (3, "assistant", "NO_ACTION:"),
        ):
            db.add_message(conn, "2ay6qic9", role=role, body=body,
                           origin_surface="scheduled", task_id=tid)

    def test_cleanup_drops_and_strips(self, conn):
        self._seed_garbage(conn)
        conn.execute(
            "DELETE FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        )
        db._migrate_scheduled_transcript_cleanup(conn)
        rows = conn.execute(
            "SELECT role, body FROM messages WHERE room_token = '2ay6qic9' "
            "ORDER BY id"
        ).fetchall()
        # user rows gone, NO_ACTION gone, ACTION stripped
        assert [(r["role"], r["body"]) for r in rows] == [
            ("assistant", "You are now at Home"),
        ]

    def test_cleanup_idempotent_and_markered(self, conn):
        self._seed_garbage(conn)
        conn.execute(
            "DELETE FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        )
        db._migrate_scheduled_transcript_cleanup(conn)
        marker = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        ).fetchone()
        assert marker is not None
        # Re-add a garbage row; a second run is a no-op (marker set).
        db.add_message(conn, "2ay6qic9", role="user", body="",
                       origin_surface="scheduled", task_id=9)
        db._migrate_scheduled_transcript_cleanup(conn)
        leftover = conn.execute(
            "SELECT 1 FROM messages WHERE room_token='2ay6qic9' AND role='user'"
        ).fetchone()
        assert leftover is not None  # not touched: cleanup already ran

    def test_cleanup_leaves_conversational_rows_alone(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        db.add_message(conn, "r", role="user", body="hi", origin_surface="talk", task_id=1)
        db.add_message(conn, "r", role="assistant", body="ACTION: not a cron",
                       origin_surface="talk", task_id=1)
        conn.execute(
            "DELETE FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        )
        db._migrate_scheduled_transcript_cleanup(conn)
        rows = conn.execute(
            "SELECT role, body FROM messages WHERE room_token='r' ORDER BY id"
        ).fetchall()
        assert [(r["role"], r["body"]) for r in rows] == [
            ("user", "hi"), ("assistant", "ACTION: not a cron"),
        ]
