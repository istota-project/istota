"""Web chat transcript survives task retention (ISSUE-126).

The web transcript used to be rebuilt from the `tasks` table, which
`cleanup_old_tasks` garbage-collects after a few days — so a dormant room's
completed turns vanished while stray cancelled/failed tasks lingered, and the
room opened to ancient out-of-context messages.

Two fixes, both covered here:
- The transcript is now read from the durable canonical `messages` store, with
  surviving `tasks` rows joined in only to enrich (trace / timing / model) or to
  fill turns the store doesn't yet hold (failed/cancelled, in-flight, legacy).
- A dormant room recovers its real history from the `talk_messages` cache, the
  only durable copy of turns whose tasks were retention-deleted before the
  unified-room-sync migration ran.
"""

import json

import pytest

from istota import db
from istota.config import Config

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web = True
except ImportError:
    _has_web = False

_needs_web = pytest.mark.skipif(not _has_web, reason="web dependencies not installed")


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _task(conn, token, prompt, result, *, source_type="talk", status="completed", user="u"):
    row = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (source_type, user, token, prompt, result, status),
    ).fetchone()
    return int(row["id"])


# --------------------------------------------------------------------------- #
# Talk-cache recovery backfill (pure DB; no web deps)
# --------------------------------------------------------------------------- #
class TestTalkCacheRecovery:
    def _tm(self, conn, token, mid, actor, text, *, mtype="comment", ref=None, ts=1000):
        conn.execute(
            "INSERT INTO talk_messages (conversation_token, message_id, actor_id, "
            "message_type, message_text, reference_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, mid, actor, mtype, text, ref, ts),
        )

    def test_recovers_paired_turn(self, conn):
        db.register_room(conn, "tok", "u", origin="talk")
        # [human] -> [bot :ack] -> [bot system "edited"] -> [bot :result]
        self._tm(conn, "tok", 1, "stefan", "what is 2+2", ref="hash1", ts=100)
        self._tm(conn, "tok", 2, "zorg", "*Thinking* #5", ref="istota:task:5:ack", ts=101)
        self._tm(conn, "tok", 3, "zorg", "You edited a message", mtype="system", ts=101)
        self._tm(conn, "tok", 4, "zorg", "It is 4.", ref="istota:task:5:result", ts=102)
        n = db.backfill_room_messages_from_talk_cache(conn, "tok")
        assert n == 2
        msgs = db.get_messages(conn, "tok")
        assert [(m.role, m.body, m.task_id) for m in msgs] == [
            ("user", "what is 2+2", 5),
            ("assistant", "It is 4.", 5),
        ]

    def test_resolves_rich_object_placeholders(self, conn):
        # ISSUE-132 — the cache holds the raw body, so a {file0} / {mention-…}
        # placeholder must be resolved against the cached messageParameters when
        # folded into the durable store, else it leaks literally into the web UI.
        db.register_room(conn, "tok", "u", origin="talk")
        conn.execute(
            "INSERT INTO talk_messages (conversation_token, message_id, actor_id, "
            "message_type, message_text, message_parameters, reference_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("tok", 1, "stefan", "comment", "look at {file0}",
             json.dumps({"file0": {"type": "file", "name": "scan.pdf"}}), "h", 100),
        )
        self._tm(conn, "tok", 2, "zorg", "Got it.", ref="istota:task:9:result", ts=102)
        db.backfill_room_messages_from_talk_cache(conn, "tok")
        bodies = [(m.role, m.body) for m in db.get_messages(conn, "tok")]
        assert bodies == [("user", "look at [scan.pdf]"), ("assistant", "Got it.")]

    def test_skips_acks_and_system_and_is_idempotent(self, conn):
        db.register_room(conn, "tok", "u", origin="talk")
        self._tm(conn, "tok", 1, "stefan", "q", ref="h", ts=100)
        self._tm(conn, "tok", 2, "zorg", "*ack* #5", ref="istota:task:5:ack", ts=101)
        self._tm(conn, "tok", 3, "zorg", "answer", ref="istota:task:5:result", ts=102)
        db.backfill_room_messages_from_talk_cache(conn, "tok")
        again = db.backfill_room_messages_from_talk_cache(conn, "tok")
        assert again == 0  # idempotent — the messages unique index backstops it
        assert [m.role for m in db.get_messages(conn, "tok")] == ["user", "assistant"]

    def test_unpaired_result_inserts_assistant_only(self, conn):
        db.register_room(conn, "tok", "u", origin="talk")
        self._tm(conn, "tok", 1, "zorg", "answer, no preceding question",
                 ref="istota:task:7:result", ts=100)
        n = db.backfill_room_messages_from_talk_cache(conn, "tok")
        assert n == 1
        assert [(m.role, m.task_id) for m in db.get_messages(conn, "tok")] == [("assistant", 7)]

    def test_historical_timestamps_preserved(self, conn):
        db.register_room(conn, "tok", "u", origin="talk")
        self._tm(conn, "tok", 1, "stefan", "q", ref="h", ts=1_700_000_000)
        self._tm(conn, "tok", 2, "zorg", "a", ref="istota:task:9:result", ts=1_700_000_050)
        db.backfill_room_messages_from_talk_cache(conn, "tok")
        msgs = db.get_messages(conn, "tok")
        # created_at is the Talk timestamp, not now() — so day-dividers / ordering
        # reflect when the turn actually happened.
        assert msgs[0].created_at.startswith("2023-11-14")

    def test_no_bot_turns_recovers_nothing(self, conn):
        db.register_room(conn, "tok", "u", origin="talk")
        self._tm(conn, "tok", 1, "stefan", "just a human message", ref="h", ts=100)
        assert db.backfill_room_messages_from_talk_cache(conn, "tok") == 0


# --------------------------------------------------------------------------- #
# Transcript loader reads the durable store (web deps)
# --------------------------------------------------------------------------- #
@_needs_web
class TestTranscriptSurvivesRetention:
    def _loader(self, db_path):
        from istota import web_app
        web_app._config = Config()
        web_app._config.db_path = db_path
        return web_app._chat_room_messages

    def test_turn_survives_task_deletion(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            t = _task(conn, "tok", "q", "a")
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=t, origin_surface="talk")
            db.store_turn_message(conn, "tok", role="assistant", body="a", task_id=t, origin_surface="talk")
            conn.execute("DELETE FROM tasks WHERE id = ?", (t,))  # retention GC
        out = self._loader(db_path)("u", "tok", 50)
        texts = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("user", "q") in texts
        assert ("assistant", "a") in texts

    def test_no_double_when_task_and_message_present(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            t = _task(conn, "tok", "q", "a")
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=t, origin_surface="talk")
            db.store_turn_message(conn, "tok", role="assistant", body="a", task_id=t, origin_surface="talk")
        out = self._loader(db_path)("u", "tok", 50)
        pairs = [(m["role"], m["text"]) for m in out["messages"]]
        assert pairs.count(("assistant", "a")) == 1
        assert pairs.count(("user", "q")) == 1

    def test_legacy_task_without_message_still_renders(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            _task(conn, "tok", "lonely q", "lonely a")
        out = self._loader(db_path)("u", "tok", 50)
        texts = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("user", "lonely q") in texts
        assert ("assistant", "lonely a") in texts

    def test_failed_task_answer_from_surviving_task_row(self, db_path):
        # A failed turn: user row in the store, no assistant row (the scheduler
        # only stores assistant turns on success). The error answer must come
        # from the retention-kept task row, not be dropped.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            t = _task(conn, "tok", "q", None, status="failed")
            conn.execute("UPDATE tasks SET error = 'boom' WHERE id = ?", (t,))
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=t, origin_surface="talk")
        out = self._loader(db_path)("u", "tok", 50)
        assert ("user", "q") in [(m["role"], m["text"]) for m in out["messages"]]
        assert any(
            m["role"] == "assistant" and "boom" in (m["text"] or "")
            for m in out["messages"]
        )

    def test_assistant_enriched_from_surviving_task(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            t = _task(conn, "tok", "q", "a")
            conn.execute(
                "UPDATE tasks SET execution_trace = ?, model_used = ?, "
                "started_at = ?, completed_at = ? WHERE id = ?",
                (
                    json.dumps([{"type": "tool", "text": "Bash: ls"},
                                {"type": "text", "text": "a"}]),
                    "claude-x", "2026-01-01 00:00:00", "2026-01-01 00:00:05", t,
                ),
            )
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=t, origin_surface="talk")
            db.store_turn_message(conn, "tok", role="assistant", body="a", task_id=t, origin_surface="talk")
        out = self._loader(db_path)("u", "tok", 50)
        asst = [m for m in out["messages"] if m["role"] == "assistant"][0]
        assert asst["model"] == "claude-x"
        assert asst["duration_seconds"] == 5.0
        assert any(s["kind"] == "tool" for s in asst["segments"])

    def test_retention_deleted_turn_renders_plain_body(self, db_path):
        # No surviving task -> assistant renders as a plain text bubble (segments
        # built from the body alone), never blank.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=999, origin_surface="talk")
            db.store_turn_message(conn, "tok", role="assistant", body="durable answer", task_id=999, origin_surface="talk")
        out = self._loader(db_path)("u", "tok", 50)
        asst = [m for m in out["messages"] if m["role"] == "assistant"][0]
        assert asst["text"] == "durable answer"
        assert asst["segments"] == [{"kind": "text", "text": "durable answer"}]

    def test_created_at_normalized_to_iso_utc(self, db_path):
        # The store holds naive-UTC timestamps (SQLite datetime('now'), and the
        # Talk-cache backfill's strftime). Returned raw, the browser's new Date()
        # parses a space-separated marker-less string as *local* time, so an
        # imported-from-Talk turn renders hours off. The loader must hand the
        # frontend an explicit ...Z UTC string (matching the live path's
        # new Date().toISOString()).
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            t = _task(conn, "tok", "q", "a")
            db.store_turn_message(conn, "tok", role="user", body="q", task_id=t, origin_surface="talk")
            db.store_turn_message(conn, "tok", role="assistant", body="a", task_id=t, origin_surface="talk")
            # Pin both turns to a known naive-UTC instant so we can assert the
            # value is reformatted, not shifted.
            conn.execute(
                "UPDATE messages SET created_at = '2023-11-14 22:13:20' WHERE room_token = ?",
                ("tok",),
            )
        out = self._loader(db_path)("u", "tok", 50)
        assert out["messages"], "expected the turn to render"
        for m in out["messages"]:
            assert m["created_at"] == "2023-11-14T22:13:20Z", m

    def test_scheduled_assistant_shown_synthetic_prompt_hidden(self, db_path):
        # Scheduled-job posts (e.g. the daily money sync) live in the store as
        # origin_surface='scheduled'. The bot's post IS shown in the room — it's
        # exactly what lands in the Talk room — but the synthetic cron *prompt*
        # (the 'user' row) is internal, never user-authored, so it stays hidden.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            db.store_turn_message(conn, "tok", role="user", body="Run sync-monarch", task_id=1, origin_surface="scheduled")
            db.store_turn_message(conn, "tok", role="assistant", body="4 new transactions", task_id=1, origin_surface="scheduled")
        out = self._loader(db_path)("u", "tok", 50)
        texts = [(m["role"], m["text"]) for m in out["messages"]]
        assert ("assistant", "4 new transactions") in texts
        assert ("user", "Run sync-monarch") not in texts
