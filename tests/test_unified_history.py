"""Stage 2 — unified history reads.

`get_conversation_history` is repointed at the canonical `messages` store with
re-pairing (row-per-message -> ConversationMessage prompt/result pairs keyed on
task_id), with a self-healing dual-read shim: it prefers `messages` only when
that store is caught up to the latest completed task for the token, otherwise it
falls back to the legacy `tasks` reconstruction so context never goes stale
mid-rollout.
"""

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


class TestMessagesEquivalence:
    def test_backfilled_history_matches_tasks(self, conn):
        db.register_room(conn, "cpz", "u", origin="talk")
        t1 = _add_task(conn, "cpz", "hi", "hello")
        t2 = _add_task(conn, "cpz", "how are you", "good")
        # Legacy path result (no messages yet -> fallback)
        legacy = db.get_conversation_history(conn, "cpz", limit=10)
        assert [(m.id, m.prompt, m.result) for m in legacy] == [
            (t1, "hi", "hello"),
            (t2, "how are you", "good"),
        ]
        # Backfill -> messages caught up -> reads messages, identical result
        db.backfill_room_messages_from_tasks(conn, "cpz")
        unified = db.get_conversation_history(conn, "cpz", limit=10)
        assert [(m.id, m.prompt, m.result) for m in unified] == [
            (t1, "hi", "hello"),
            (t2, "how are you", "good"),
        ]
        assert [m.source_type for m in unified] == ["talk", "talk"]

    def test_oldest_first_ordering_preserved(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        ids = [_add_task(conn, "r", f"q{i}", f"a{i}", source_type="web") for i in range(5)]
        db.backfill_room_messages_from_tasks(conn, "r")
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [m.id for m in hist] == ids

    def test_cross_surface_history_in_one_room(self, conn):
        # A Talk room continued in web: both share the token, both appear.
        db.register_room(conn, "cpz", "u", origin="talk")
        t1 = _add_task(conn, "cpz", "talk q", "talk a", source_type="talk")
        t2 = _add_task(conn, "cpz", "web q", "web a", source_type="web")
        db.backfill_room_messages_from_tasks(conn, "cpz")
        hist = db.get_conversation_history(conn, "cpz", limit=10)
        assert [(m.id, m.source_type) for m in hist] == [(t1, "talk"), (t2, "web")]


class TestStalenessFallback:
    def test_falls_back_to_tasks_when_messages_stale(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1")
        t2 = _add_task(conn, "r", "q2", "a2")
        db.backfill_room_messages_from_tasks(conn, "r")
        # A new task completes but its assistant row is NOT in messages yet
        # (simulates the rollout window before live assistant writes land).
        t3 = _add_task(conn, "r", "q3", "a3")
        hist = db.get_conversation_history(conn, "r", limit=10)
        # Must include t3 — falls back to tasks rather than serving stale messages.
        assert [m.id for m in hist] == [t1, t2, t3]

    def test_empty_room_returns_empty(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        assert db.get_conversation_history(conn, "r", limit=10) == []


class TestRepairingSemantics:
    def test_inflight_turn_excluded(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        t1 = _add_task(conn, "r", "done", "answer", source_type="web")
        # An in-flight turn: user message stored, task still running, no result.
        running = _add_task(conn, "r", "pending", None, source_type="web", status="running")
        db.add_message(conn, "r", role="user", body="pending", origin_surface="web", task_id=running)
        db.backfill_room_messages_from_tasks(conn, "r")
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [m.id for m in hist] == [t1]

    def test_exclude_task_id_honored(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        t1 = _add_task(conn, "r", "q1", "a1", source_type="web")
        t2 = _add_task(conn, "r", "q2", "a2", source_type="web")
        db.backfill_room_messages_from_tasks(conn, "r")
        hist = db.get_conversation_history(conn, "r", limit=10, exclude_task_id=t2)
        assert [m.id for m in hist] == [t1]

    def test_exclude_source_types_honored(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task(conn, "r", "q1", "a1", source_type="talk")
        sched = _add_task(conn, "r", "cron", "ran", source_type="scheduled")
        db.backfill_room_messages_from_tasks(conn, "r")
        hist = db.get_conversation_history(
            conn, "r", limit=10, exclude_source_types=["scheduled", "briefing"]
        )
        assert [m.id for m in hist] == [t1]


class TestBackfill:
    def test_backfill_idempotent(self, conn):
        db.register_room(conn, "r", "u", origin="web")
        _add_task(conn, "r", "q", "a", source_type="web")
        db.backfill_room_messages_from_tasks(conn, "r")
        db.backfill_room_messages_from_tasks(conn, "r")
        msgs = db.get_messages(conn, "r")
        assert [m.role for m in msgs] == ["user", "assistant"]

    def test_migration_backfills_registered_rooms(self, tmp_path):
        # Full migration path: legacy talk tasks -> rooms + backfilled messages.
        db_path = tmp_path / "x.db"
        db.init_db(db_path)
        with db.get_db(db_path) as c:
            c.execute("DELETE FROM _migration_state WHERE name = 'unified_rooms_v1'")
            _add_task(c, "cpz", "hi", "hello", source_type="talk")
            c.commit()
            db._migrate_unified_rooms(c)
            c.commit()
            hist = db.get_conversation_history(c, "cpz", limit=10)
            assert [(m.prompt, m.result) for m in hist] == [("hi", "hello")]
