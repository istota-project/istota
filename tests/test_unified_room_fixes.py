"""Regression coverage for the unified Talk/web room sync post-review fixes.

Mulder/Scully findings on the unified-rooms feature:
  M1 — the dual-read was all-or-nothing (keyed on the *single newest* completed
       task), so a partially-populated `messages` store silently dropped older
       turns from LLM context.
  M2 — `_migrate_unified_rooms` set its completion marker even after a real
       (non-"no such table") failure swallowed mid-backfill, so the partial
       state was never retried.
  H2 — the `messages` unique index keyed on `origin_surface`, looser than every
       app-level idempotency guard, so a duplicate turn could slip past it.
  Orphaned Talk rooms — a Talk conversation deleted in Nextcloud kept surfacing
       in the web room list because its registry row was never reconciled.
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


def _add_task(conn, token, prompt, result, *, source_type="talk", status="completed"):
    row = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status) VALUES (?, 'u', ?, ?, ?, ?) RETURNING id",
        (source_type, token, prompt, result, status),
    ).fetchone()
    return int(row["id"])


class TestM1PartialPopulationDualRead:
    def test_partial_population_does_not_drop_older_turns(self, conn):
        # Three completed turns, but only the NEWEST is mirrored into messages
        # (the partial-migration / mid-rollout state). History must still return
        # all three — the dual-read falls back to the always-complete tasks path
        # rather than serving only the subset already in messages.
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1")
        t2 = _add_task(conn, "r", "q2", "a2")
        t3 = _add_task(conn, "r", "q3", "a3")
        db.store_turn_message(conn, "r", role="user", body="q3", task_id=t3, origin_surface="talk")
        db.store_turn_message(conn, "r", role="assistant", body="a3", task_id=t3, origin_surface="talk")

        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [m.id for m in hist] == [t1, t2, t3]

    def test_middle_turn_missing_falls_back(self, conn):
        # Newest + oldest mirrored, the middle turn missing: still not caught up.
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1")
        t2 = _add_task(conn, "r", "q2", "a2")
        t3 = _add_task(conn, "r", "q3", "a3")
        for role, body, tid in (("user", "q1", t1), ("assistant", "a1", t1),
                                ("user", "q3", t3), ("assistant", "a3", t3)):
            db.store_turn_message(conn, "r", role=role, body=body, task_id=tid, origin_surface="talk")

        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [m.id for m in hist] == [t1, t2, t3]

    def test_fully_populated_reads_from_messages(self, conn):
        # Equivalence still holds: once every turn is mirrored, the messages path
        # serves identical history.
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1")
        t2 = _add_task(conn, "r", "q2", "a2")
        db.backfill_room_messages_from_tasks(conn, "r")
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [(m.id, m.prompt, m.result) for m in hist] == [
            (t1, "q1", "a1"),
            (t2, "q2", "a2"),
        ]


class TestM2MigrationMarkerOnFailure:
    class _FailOn:
        """Connection proxy that raises a real OperationalError on a statement
        whose text contains ``needle`` — to simulate a mid-backfill disk error."""

        def __init__(self, real, needle):
            self._real = real
            self._needle = needle

        def execute(self, sql, *args):
            if self._needle in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def test_marker_not_set_after_real_failure(self, conn):
        conn.execute("DELETE FROM _migration_state WHERE name = 'unified_rooms_v1'")
        _add_task(conn, "cpz", "hi", "hello", source_type="talk")
        # Fail the turn-backfill step specifically.
        proxy = self._FailOn(conn, "SELECT conversation_token, ?,")
        db._migrate_unified_rooms(proxy)  # must not raise
        present = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'unified_rooms_v1'"
        ).fetchone()
        assert present is None  # left unset so the next boot retries

    def test_retry_after_failure_completes(self, conn):
        conn.execute("DELETE FROM _migration_state WHERE name = 'unified_rooms_v1'")
        _add_task(conn, "cpz", "hi", "hello", source_type="talk")
        proxy = self._FailOn(conn, "SELECT conversation_token, ?,")
        db._migrate_unified_rooms(proxy)
        # Clean re-run (no fault injection) completes and backfills.
        db._migrate_unified_rooms(conn)
        assert conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'unified_rooms_v1'"
        ).fetchone() is not None
        hist = db.get_conversation_history(conn, "cpz", limit=10)
        assert [(m.prompt, m.result) for m in hist] == [("hi", "hello")]

    def test_missing_legacy_table_is_benign(self, conn):
        # A genuinely-absent legacy table ("no such table") must NOT block the
        # marker — that's the fresh-install path.
        conn.execute("DROP TABLE web_chat_messages")
        conn.execute("DELETE FROM _migration_state WHERE name = 'unified_rooms_v1'")
        db._migrate_unified_rooms(conn)
        assert conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'unified_rooms_v1'"
        ).fetchone() is not None


class TestH2MessagesUniqueIndex:
    def test_duplicate_turn_rejected_regardless_of_origin_surface(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        t = _add_task(conn, "r", "q", "a")
        db.add_message(conn, "r", role="assistant", body="a", origin_surface="talk", task_id=t)
        with pytest.raises(sqlite3.IntegrityError):
            db.add_message(conn, "r", role="assistant", body="a2", origin_surface="web", task_id=t)

    def test_user_and_assistant_rows_coexist(self, conn):
        # The index must still permit one user + one assistant row per turn.
        db.register_room(conn, "r", "u", origin="talk")
        t = _add_task(conn, "r", "q", "a")
        db.add_message(conn, "r", role="user", body="q", origin_surface="talk", task_id=t)
        db.add_message(conn, "r", role="assistant", body="a", origin_surface="talk", task_id=t)
        assert [m.role for m in db.get_messages(conn, "r")] == ["user", "assistant"]


class TestOrphanedTalkRooms:
    def test_archives_talk_rooms_absent_from_live_set(self, conn):
        db.register_room(conn, "live", "u", origin="talk")
        db.register_room(conn, "gone", "u", origin="talk")
        db.register_room(conn, "webroom", "u", origin="web")
        n = db.archive_orphaned_talk_rooms(conn, {"live"})
        assert n == 1
        # The deleted Talk room drops out; the live Talk room and the web room stay.
        assert {r.token for r in db.list_rooms(conn, "u")} == {"live", "webroom"}

    def test_idempotent(self, conn):
        db.register_room(conn, "gone", "u", origin="talk")
        assert db.archive_orphaned_talk_rooms(conn, set()) == 1
        assert db.archive_orphaned_talk_rooms(conn, set()) == 0

    def test_does_not_touch_web_rooms(self, conn):
        db.register_room(conn, "w", "u", origin="web")
        assert db.archive_orphaned_talk_rooms(conn, set()) == 0
        assert {r.token for r in db.list_rooms(conn, "u")} == {"w"}
