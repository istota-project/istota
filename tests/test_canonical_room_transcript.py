"""Canonical room transcript for all source types (ISSUE-176).

Any bot post delivered into a web-visible room is stored as an assistant spine
row and rendered as a bot turn, whatever the source type produced it — subtask,
scheduled, briefing, heartbeat. Covers:

- the general `_store_room_turn` producer (replacing the two special-case helpers),
- the generalized `TRANSCRIPT_SURFACE_FILTER` (assistant-any / user-conversational),
- the `nonconversational_transcript_cleanup_v1` migration that tames the rows the
  earlier blanket backfill folded in (drop synthetic user rows, normalize briefing
  bodies), restoring the "user rows conversational-only" invariant,
- that the extra assistant rows never leak into LLM context or the caught-up check.
"""

from types import SimpleNamespace

import pytest

from istota import db
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


def _task(token, *, source_type="subtask", task_id=10):
    return SimpleNamespace(
        source_type=source_type, conversation_token=token, id=task_id,
    )


def _add_task_row(conn, token, prompt, result, *, source_type="talk",
                  status="completed", heartbeat_silent=0):
    row = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status, heartbeat_silent) VALUES (?, 'u', ?, ?, ?, ?, ?) "
        "RETURNING id",
        (source_type, token, prompt, result, status, heartbeat_silent),
    ).fetchone()
    return int(row["id"])


class TestStoreRoomTurn:
    def test_stores_subtask_assistant_turn(self, conn):

        db.register_room(conn, "kvcnr723", "u", origin="talk")
        _store_room_turn(conn, _task("kvcnr723", source_type="subtask"),
                         "Le Shrub: risk-off, trimming duration")
        msgs = db.get_messages(conn, "kvcnr723")
        assert [(m.role, m.body, m.origin_surface) for m in msgs] == [
            ("assistant", "Le Shrub: risk-off, trimming duration", "subtask"),
        ]

    def test_origin_surface_records_real_source_type(self, conn):
        # A briefing / heartbeat / email post records its own provenance, not a
        # hardcoded surface — visibility is not gated on it under the new filter.
        db.register_room(conn, "r", "u", origin="talk")
        _store_room_turn(conn, _task("r", source_type="briefing"), "morning brief")
        _store_room_turn(conn, _task("r", source_type="email", task_id=11), "reply body")
        msgs = db.get_messages(conn, "r")
        assert [(m.origin_surface, m.body) for m in msgs] == [
            ("briefing", "morning brief"), ("email", "reply body"),
        ]

    def test_noop_when_room_not_registered(self, conn):
        _store_room_turn(conn, _task("ghosttoken"), "alert")
        assert db.get_messages(conn, "ghosttoken") == []

    def test_noop_when_no_conversation_token(self, conn):
        _store_room_turn(conn, _task(None), "x")  # nothing raised, nothing stored

    def test_idempotent_across_retries(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        task = _task("r", task_id=42)
        _store_room_turn(conn, task, "block")
        _store_room_turn(conn, task, "block")  # retry re-completes
        assert len(db.get_messages(conn, "r")) == 1

    def test_scheduled_parity(self, conn):
        # ISSUE-133 non-regression via the general helper.
        db.register_room(conn, "r", "u", origin="talk")
        _store_room_turn(conn, _task("r", source_type="scheduled"), "you are now at home")
        msgs = db.get_messages(conn, "r")
        assert [(m.role, m.body, m.origin_surface) for m in msgs] == [
            ("assistant", "you are now at home", "scheduled"),
        ]

    def test_helper_returns_new_id_then_none_on_dup(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        task = _task("r", task_id=7)
        first = _store_room_turn(conn, task, "b")
        assert isinstance(first, int)
        assert _store_room_turn(conn, task, "b") is None

    def test_old_helpers_are_gone(self):
        import istota.scheduler as sched
        assert not hasattr(sched, "_store_scheduled_room_turn")
        assert not hasattr(sched, "_store_web_room_turn")


class TestTranscriptSurfaceFilter:
    def _rendered(self, conn, token):
        sql = (
            f"SELECT m.role AS role, m.body AS body FROM messages m "
            f"WHERE m.room_token = ? AND {db.TRANSCRIPT_SURFACE_FILTER} "
            f"ORDER BY m.id"
        )
        return [(r["role"], r["body"]) for r in conn.execute(sql, (token,)).fetchall()]

    def test_admits_any_assistant_row(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        for i, surface in enumerate(("subtask", "briefing", "heartbeat", "scheduled", "web", "talk")):
            db.add_message(conn, "r", role="assistant", body=surface,
                           origin_surface=surface, task_id=i + 1)
        assert self._rendered(conn, "r") == [
            ("assistant", s) for s in
            ("subtask", "briefing", "heartbeat", "scheduled", "web", "talk")
        ]

    def test_user_rows_only_conversational(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        db.add_message(conn, "r", role="user", body="talk-u", origin_surface="talk", task_id=1)
        db.add_message(conn, "r", role="user", body="web-u", origin_surface="web", task_id=2)
        # A stray non-conversational user row must never render (belt-and-suspenders).
        db.add_message(conn, "r", role="user", body="sub-u", origin_surface="subtask", task_id=3)
        assert self._rendered(conn, "r") == [("user", "talk-u"), ("user", "web-u")]


class TestNonconversationalCleanupMigration:
    MARKER = "nonconversational_transcript_cleanup_v1"

    def _seed(self, conn):
        db.register_room(conn, "kvcnr723", "u", origin="talk")
        rows = [
            # subtask: synthetic user prompt + plain-text assistant block
            (1, "user", "subtask", "run extraction"),
            (1, "assistant", "subtask", "Le Shrub block, already plain"),
            # briefing: synthetic user + raw JSON assistant
            (2, "user", "briefing", "generate briefing"),
            (2, "assistant", "briefing", '{"subject":"AM","body":"the delivered body"}'),
            # briefing whose result is not JSON → left as-is
            (3, "user", "briefing", ""),
            (3, "assistant", "briefing", "📰 plain briefing text"),
            # conversational rows — untouched
            (4, "user", "talk", "hi"),
            (4, "assistant", "talk", "hello"),
            # scheduled — owned by its own marker, untouched here
            (5, "assistant", "scheduled", "cron post"),
            # system note lane — untouched
            (6, "system", "web", "an alert"),
        ]
        for tid, role, surface, body in rows:
            db.add_message(conn, "kvcnr723", role=role, body=body,
                           origin_surface=surface, task_id=tid)

    def _run(self, conn):
        conn.execute("DELETE FROM _migration_state WHERE name = ?", (self.MARKER,))
        db._migrate_nonconversational_transcript_cleanup(conn)

    def test_drops_nonconversational_user_rows(self, conn):
        self._seed(conn)
        self._run(conn)
        users = conn.execute(
            "SELECT origin_surface FROM messages WHERE room_token='kvcnr723' "
            "AND role='user' ORDER BY id"
        ).fetchall()
        # only the conversational (talk) user row survives
        assert [r["origin_surface"] for r in users] == ["talk"]

    def test_normalizes_briefing_json_body(self, conn):
        self._seed(conn)
        self._run(conn)
        row = conn.execute(
            "SELECT body FROM messages WHERE room_token='kvcnr723' "
            "AND role='assistant' AND origin_surface='briefing' "
            "AND task_id=2"
        ).fetchone()
        assert row["body"] == "the delivered body"

    def test_leaves_unparseable_briefing_as_is(self, conn):
        self._seed(conn)
        self._run(conn)
        row = conn.execute(
            "SELECT body FROM messages WHERE room_token='kvcnr723' "
            "AND role='assistant' AND origin_surface='briefing' AND task_id=3"
        ).fetchone()
        assert row["body"] == "📰 plain briefing text"

    def test_leaves_subtask_body_as_is(self, conn):
        self._seed(conn)
        self._run(conn)
        row = conn.execute(
            "SELECT body FROM messages WHERE room_token='kvcnr723' "
            "AND role='assistant' AND origin_surface='subtask'"
        ).fetchone()
        assert row["body"] == "Le Shrub block, already plain"

    def test_leaves_conversational_and_scheduled_and_system_alone(self, conn):
        self._seed(conn)
        self._run(conn)
        survivors = conn.execute(
            "SELECT role, origin_surface, body FROM messages "
            "WHERE room_token='kvcnr723' AND origin_surface IN ('talk','scheduled','web') "
            "ORDER BY id"
        ).fetchall()
        assert [(r["role"], r["origin_surface"], r["body"]) for r in survivors] == [
            ("user", "talk", "hi"),
            ("assistant", "talk", "hello"),
            ("assistant", "scheduled", "cron post"),
            ("system", "web", "an alert"),
        ]

    def test_idempotent_and_markered(self, conn):
        self._seed(conn)
        self._run(conn)
        marker = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = ?", (self.MARKER,)
        ).fetchone()
        assert marker is not None
        # A second run (marker set) is a no-op: a freshly-added garbage row survives.
        db.add_message(conn, "kvcnr723", role="user", body="new",
                       origin_surface="subtask", task_id=99)
        db._migrate_nonconversational_transcript_cleanup(conn)
        leftover = conn.execute(
            "SELECT 1 FROM messages WHERE room_token='kvcnr723' "
            "AND role='user' AND origin_surface='subtask' AND task_id=99"
        ).fetchone()
        assert leftover is not None

    def test_import_failure_aborts_before_any_mutation(self, conn, monkeypatch):
        # The briefing parser is resolved before the user-row DELETE, so a
        # mid-migration import failure leaves ZERO mutation and no marker — the
        # unmarked retry re-applies the whole thing (no half-applied reveal).
        self._seed(conn)
        conn.execute("DELETE FROM _migration_state WHERE name = ?", (self.MARKER,))

        import builtins
        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name == "istota.skills.briefing" or name.endswith("skills.briefing"):
                raise ImportError("simulated broken import")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", boom)
        db._migrate_nonconversational_transcript_cleanup(conn)
        monkeypatch.undo()

        # No mutation: the synthetic subtask/briefing user rows still present.
        user_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE room_token='kvcnr723' "
            "AND role='user' AND origin_surface NOT IN ('web','talk','scheduled')"
        ).fetchone()
        assert user_rows["n"] > 0
        # No marker: a healthy retry will re-apply.
        marker = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = ?", (self.MARKER,)
        ).fetchone()
        assert marker is None


class TestLLMContextIsolation:
    def test_subtask_assistant_row_absent_from_context(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        t1 = _add_task_row(conn, "r", "q1", "a1", source_type="talk")
        db.backfill_room_messages_from_tasks(conn, "r")  # mirrors the talk turn
        # A live subtask post, assistant-only (no user row) via the producer.
        _store_room_turn(conn, _task("r", source_type="subtask", task_id=999),
                         "cron block")
        hist = db.get_conversation_history(conn, "r", limit=10)
        assert [(m.id, m.prompt, m.result) for m in hist] == [(t1, "q1", "a1")]

    def test_caught_up_unaffected_by_subtask_row(self, conn):
        db.register_room(conn, "r", "u", origin="talk")
        _add_task_row(conn, "r", "q1", "a1", source_type="talk")
        db.backfill_room_messages_from_tasks(conn, "r")
        assert db._messages_caught_up(conn, "r") is True
        _store_room_turn(conn, _task("r", source_type="subtask", task_id=999), "block")
        assert db._messages_caught_up(conn, "r") is True

    def test_build_db_context_excludes_subtask_and_heartbeat(self, db_path):
        # The email / Talk-API-fallback context builder must not re-surface a
        # subtask's synthetic prompt via get_previous_tasks (which deliberately
        # re-injects scheduled/briefing but must hard-exclude subtask/heartbeat).
        from istota import executor
        from istota.config import Config

        with db.get_db(db_path) as conn:
            db.register_room(conn, "R", "u", origin="talk")
            t1 = _add_task_row(conn, "R", "q1", "a1", source_type="talk")
            _add_task_row(conn, "R", "INTERNAL-extraction", "NEWSLETTER-BLOCK",
                          source_type="subtask")
            _add_task_row(conn, "R", "hb-prompt", "hb-result", source_type="heartbeat")
            _add_task_row(conn, "R", "cron-prompt", "CRON-DIGEST", source_type="scheduled")

        cfg = Config()
        cfg.db_path = db_path
        task = SimpleNamespace(
            id=999, conversation_token="R", reply_to_talk_id=None,
            user_id="u", source_type="email", prompt="new email",
        )
        with db.get_db(db_path) as conn:
            ctx, ids = executor._build_db_context(task, cfg, conn)
        ctx = ctx or ""
        # conversational turn present; subtask/heartbeat internal content absent
        assert "a1" in ctx
        assert "INTERNAL-extraction" not in ctx
        assert "NEWSLETTER-BLOCK" not in ctx
        assert "hb-result" not in ctx
        # scheduled IS still deliberately re-surfaced (existing feature)
        assert "CRON-DIGEST" in ctx

    def test_get_previous_tasks_exclude_source_types(self, conn):
        db.register_room(conn, "R", "u", origin="talk")
        _add_task_row(conn, "R", "q1", "a1", source_type="talk")
        _add_task_row(conn, "R", "sub-p", "sub-r", source_type="subtask")
        _add_task_row(conn, "R", "cron-p", "cron-r", source_type="scheduled")
        prev = db.get_previous_tasks(
            conn, "R", limit=10, exclude_source_types=["subtask", "heartbeat"],
        )
        types = {m.source_type for m in prev}
        assert "subtask" not in types
        assert {"talk", "scheduled"} <= types


@_needs_web_deps
class TestWebReaderRendersNonconversationalPost:
    def test_subtask_post_renders_as_bot_bubble(self, db_path):
        from istota import web_app
        from istota.config import Config

        web_app._config = Config()
        web_app._config.db_path = db_path
        with db.get_db(db_path) as conn:
            db.register_room(conn, "kvcnr723", "u", origin="talk")
            db.add_room_binding(conn, "kvcnr723", "talk", "kvcnr723")
            _store_room_turn(
                conn, _task("kvcnr723", source_type="subtask"), "the letter block",
            )
        out = web_app._chat_room_messages("u", "kvcnr723", 50)
        rendered = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("assistant", "the letter block") in rendered
        assert all(r != "user" for r, _ in rendered)


class TestReadSyncStamp:
    """The stuck-unread safety for a room-bound non-conversational post rides the
    `"talk"` external-id stamp, not role/origin. A completed subtask delivered to
    its own Talk channel gets its assistant `messages` row stamped, and the
    Talk→web read cap (`room_max_talk_synced_message_id`) then advances to it —
    so a Talk read clears it in web. Pins the safety to the stamp so a future
    stamp-guard narrowing fails loudly instead of leaving subtask rows unread."""

    def test_stamped_subtask_row_advances_talk_read_cap(self, conn):
        db.register_room(conn, "kvcnr723", "u", origin="talk")
        mid = _store_room_turn(
            conn, _task("kvcnr723", source_type="subtask", task_id=55),
            "the letter block",
        )
        # Before the stamp the cap is 0 (nothing demonstrably in Talk yet).
        assert db.room_max_talk_synced_message_id(conn, "kvcnr723") == 0
        # The scheduler stamps the Talk-leg assistant row after delivery.
        db.set_message_external_id(conn, mid, "talk", "9001")
        assert db.room_max_talk_synced_message_id(conn, "kvcnr723") == mid


@_needs_web_deps
class TestCrossRoomViewExpansion:
    def test_subtask_row_is_starrable_across_rooms(self, db_path):

        with db.get_db(db_path) as conn:
            db.register_room(conn, "r", "u", origin="talk")
            db.add_room_member(conn, "r", "u")
            mid = _store_room_turn(
                conn, _task("r", source_type="subtask"), "cross-room block",
            )
            rows = db.list_messages_across_rooms(conn, "u", view="all", limit=50)
            bodies = [r["body"] for r in rows]
            assert "cross-room block" in bodies
            assert db.set_message_starred(conn, mid, "u", True) is True
            starred = db.list_messages_across_rooms(conn, "u", view="starred", limit=50)
            assert [r["body"] for r in starred] == ["cross-room block"]
