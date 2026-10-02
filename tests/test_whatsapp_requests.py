"""Durable WhatsApp request identity, migration and ownership."""

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from istota import db


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return path


def task(conn, user="alice"):
    ident = db.create_task(conn, user_id=user, source_type="web", prompt="test")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


def store(conn, task_id, **overrides):
    from istota.relay.requests import _store_request
    args = dict(actor_user_id="alice", task_id=task_id, request_key="one",
                kind="self_send", recipient_user_id="alice", text="hello",
                service_body="hello", template_body=None,
                provider="baileys", binding_fingerprint="binding-hash")
    args.update(overrides)
    return _store_request(conn, **args)


def test_fresh_schema_and_upgrade_match(tmp_path, path):
    old = tmp_path / "old.db"
    schema = (Path(__file__).parents[1] / "schema.sql").read_text()
    schema = schema.split("-- Durable WhatsApp skill requests and relays")[0]
    schema = schema.replace("    whatsapp_confirmation_request_id TEXT,\n", "")
    schema = schema.replace("    delivery_reference TEXT,\n", "")
    with sqlite3.connect(old) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt) VALUES (1,'alice','web','existing')")
    db.init_db(old)
    db.init_db(old)
    tables = ("tasks", "messages", "whatsapp_skill_requests", "message_relays",
              "relay_blocks", "relay_reply_candidates")
    with db.get_db(path) as fresh, db.get_db(old) as upgraded:
        for table in tables:
            a = {r[1]: tuple(r)[2:5] for r in fresh.execute(f"PRAGMA table_info({table})")}
            b = {r[1]: tuple(r)[2:5] for r in upgraded.execute(f"PRAGMA table_info({table})")}
            assert a == b
        assert "whatsapp_confirmation_request_id" in {r[1] for r in upgraded.execute("PRAGMA table_info(tasks)")}
        assert upgraded.execute("SELECT prompt FROM tasks WHERE id=1").fetchone()[0] == "existing"
        assert upgraded.execute("SELECT name FROM sqlite_master WHERE name='idx_messages_delivery_reference'").fetchone()


def test_replay_scope_conflict_and_rollback(path):
    from istota.relay.requests import RequestError, get_request
    with db.get_db(path) as conn:
        ident = task(conn)
        first = store(conn, ident)
        assert store(conn, ident)["id"] == first["id"]
        with pytest.raises(RequestError, match="request_conflict"):
            store(conn, ident, text="different")
        assert get_request(conn, actor_user_id="bob", request_id=first["id"]) is None
        public = get_request(conn, actor_user_id="alice", request_id=first["id"])
        assert public["text"] == "hello"
        assert "binding_fingerprint" not in public and "origin_task_id" not in public
        with pytest.raises(RequestError, match="task_unavailable"):
            store(conn, ident, actor_user_id="bob", recipient_user_id="bob")
        conn.rollback()
    with db.get_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 0


def test_concurrent_keys_create_one_record(path):
    with db.get_db(path) as conn:
        ident = task(conn)
    barrier = Barrier(2)
    def insert():
        with db.get_db(path) as conn:
            barrier.wait()
            return store(conn, ident)["id"]
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: insert(), range(2)))
    assert ids[0] == ids[1]


@pytest.mark.parametrize("key,text", [("", "ok"), ("bad key", "ok"), ("a"*65, "ok"), ("ok", " "), ("ok", "x"*2001), ("ok", "\x00\x01")])
def test_input_bounds(path, key, text):
    from istota.relay.requests import RequestError
    with db.get_db(path) as conn:
        with pytest.raises(RequestError):
            store(conn, task(conn), request_key=key, text=text)


def test_task_deletion_and_cleanup_leave_identity(path):
    from istota.relay.requests import cleanup_content, get_request
    with db.get_db(path) as conn:
        ident = task(conn)
        req = store(conn, ident)
        conn.execute("UPDATE whatsapp_skill_requests SET state='sent', closed_at=datetime('now','-31 days') WHERE id=?", (req["id"],))
        assert cleanup_content(conn, limit=1) == 1
        row = conn.execute("SELECT * FROM whatsapp_skill_requests").fetchone()
        assert row["text"] is None and row["service_body"] is None
        assert row["content_hash"] and row["service_hash"]
        assert store(conn, ident)["id"] == req["id"]
        conn.execute("DELETE FROM tasks WHERE id=?", (ident,))
        assert conn.execute("SELECT origin_task_id FROM whatsapp_skill_requests").fetchone()[0] is None
        assert get_request(conn, actor_user_id="alice", request_id=req["id"])["state"] == "sent"


def test_cleanup_does_not_commit_callers_transaction(path):
    from istota.relay.requests import cleanup_content
    with db.get_db(path) as conn:
        ident = task(conn)
        store(conn, ident)
    with db.get_db(path) as conn:
        conn.execute("UPDATE whatsapp_skill_requests SET state='failed', closed_at=datetime('now','-31 days')")
        cleanup_content(conn)
        conn.rollback()
        assert conn.execute("SELECT text FROM whatsapp_skill_requests").fetchone()[0] == "hello"


def test_canonical_reference_unique_but_nullable(path):
    with db.get_db(path) as conn:
        sql = "INSERT INTO messages (room_token,role,body,origin_surface,delivery_reference) VALUES ('room','assistant','answer','web',?)"
        conn.execute(sql, (None,))
        conn.execute(sql, (None,))
        conn.execute(sql, ("relay-return:one",))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("relay-return:one",))


# The table as it stood before the room/SMS rebuild, `return_claimed_at` still
# to be added by `_add_columns`.
_PRE_REBUILD_RELAYS = """CREATE TABLE message_relays (
    id TEXT PRIMARY KEY,
    asker_user_id TEXT NOT NULL,
    recipient_user_id TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT 'whatsapp' CHECK (surface = 'whatsapp'),
    request_id TEXT NOT NULL UNIQUE REFERENCES whatsapp_skill_requests(id),
    question TEXT,
    asker_display TEXT,
    origin TEXT,
    audience TEXT,
    provider TEXT NOT NULL,
    binding_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held','queued','sending','waiting','uncertain','answered','failed','cancelled','expired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    expires_at TEXT,
    answered_at TEXT,
    closed_at TEXT,
    inbound_answer_id TEXT,
    answer_text TEXT,
    recipient_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    return_state TEXT NOT NULL DEFAULT 'none' CHECK (return_state IN ('none','pending','sending','delivered','blocked','uncertain','expired')),
    return_reference TEXT UNIQUE,
    return_message_id TEXT,
    return_error TEXT,
    content_expires_at TEXT,
    content_cleared_at TEXT,
    CHECK (asker_user_id != recipient_user_id)
)"""


def _normalized_sql(sql):
    sql = re.sub(r"--[^\n]*", "", sql or "")
    sql = sql.replace('"', "").replace(" IF NOT EXISTS", "")
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


def _relay_schema(conn):
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='message_relays' "
        "AND sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], _normalized_sql(r[2])) for r in rows]


def _old_relays_db(tmp_path, *, orphan=False):
    old = tmp_path / "old.db"
    db.init_db(old)
    with db.get_db(old) as conn:
        conn.execute("DROP TABLE message_relays")
        conn.execute(_PRE_REBUILD_RELAYS)
        conn.execute("CREATE UNIQUE INDEX idx_message_relay_open_pair ON message_relays(asker_user_id, recipient_user_id) WHERE state IN ('held','queued','sending','waiting','uncertain')")
        conn.execute("CREATE INDEX idx_message_relay_expiry ON message_relays(state, expires_at)")
        conn.execute("CREATE INDEX idx_message_relay_return ON message_relays(return_state, answered_at)")
        asker_task = task(conn)
        bob_task = task(conn, user="bob")
        rows = [
            ("r-held", "carol", "held", None, None),
            ("r-waiting", "dave", "waiting", "2026-01-02", None),
            ("r-answered", "bob", "answered", "2026-01-03", bob_task),
            ("r-cancelled", "erin", "cancelled", None, None),
        ]
        for ident, recipient, state, approved, rtask in rows:
            # Any request row satisfies the foreign key; a self-send needs no preview.
            req = store(conn, asker_task, request_key=ident)
            request_id = "orphan-request" if orphan and state == "held" else req["id"]
            conn.execute("UPDATE whatsapp_skill_requests SET state=? WHERE id=?",
                         ("held" if state == "held" else "sent", req["id"]))
            conn.execute(
                "INSERT INTO message_relays (id,asker_user_id,recipient_user_id,request_id,"
                "question,provider,binding_fingerprint,state,approved_at,recipient_task_id) "
                "VALUES (?,'alice',?,?,?,'baileys','fp',?,?,?)",
                (ident, recipient, request_id, f"question {ident}", state, approved, rtask),
            )
    return old, asker_task, bob_task


def test_relay_rebuild_upgrades_rows_and_matches_fresh(tmp_path, path):
    old, asker_task, bob_task = _old_relays_db(tmp_path)
    db.init_db(old)
    with db.get_db(old) as conn:
        first_schema = _relay_schema(conn)
        first_rows = [tuple(r) for r in conn.execute("SELECT * FROM message_relays ORDER BY id")]
    db.init_db(old)
    with db.get_db(path) as fresh, db.get_db(old) as conn:
        assert _relay_schema(conn) == first_schema == _relay_schema(fresh)
        assert [tuple(r) for r in conn.execute("SELECT * FROM message_relays ORDER BY id")] == first_rows
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM message_relays")}
        assert set(rows) == {"r-held", "r-waiting", "r-answered", "r-cancelled"}
        assert rows["r-waiting"]["question"] == "question r-waiting"
        assert rows["r-answered"]["recipient_task_id"] == bob_task
        assert {r["surface"] for r in rows.values()} == {"whatsapp"}
        assert all(json.loads(r["destination"]) == {"kind": "whatsapp"} for r in rows.values())
        assert {k: r["approval"] for k, r in rows.items()} == {
            "r-held": None, "r-waiting": "user", "r-answered": "user", "r-cancelled": None}
        assert rows["r-held"]["question_message_id"] is None
        assert rows["r-held"]["question_talk_id"] is None

        conn.execute("UPDATE message_relays SET surface='room', approval='clean_turn' WHERE id='r-cancelled'")
        conn.execute("UPDATE message_relays SET surface='sms' WHERE id='r-cancelled'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE message_relays SET surface='email' WHERE id='r-cancelled'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE message_relays SET approval='auto' WHERE id='r-cancelled'")

        # The open-pair index still refuses a second open relay for alice -> dave.
        req = store(conn, asker_task, request_key="second")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_relays (id,asker_user_id,recipient_user_id,request_id,"
                "provider,binding_fingerprint,state) VALUES ('r-2','alice','dave',?,'baileys','fp','held')",
                (req["id"],),
            )

        # The task-delete trigger still reaches the rebuilt table.
        conn.execute("DELETE FROM tasks WHERE id=?", (bob_task,))
        assert conn.execute(
            "SELECT recipient_task_id FROM message_relays WHERE id='r-answered'").fetchone()[0] is None
        conn.execute("DELETE FROM tasks WHERE id=?", (asker_task,))
        assert conn.execute(
            "SELECT state FROM message_relays WHERE id='r-held'").fetchone()[0] == "cancelled"


def test_relay_rebuild_refuses_a_foreign_key_violation(tmp_path):
    old, _, _ = _old_relays_db(tmp_path, orphan=True)
    db.init_db(old)
    with db.get_db(old) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_relays'").fetchone()[0]
        assert "surface = 'whatsapp'" in sql
        assert conn.execute("SELECT count(*) FROM message_relays").fetchone()[0] == 4
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_relays_rebuild'").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_message_relay_open_pair'").fetchone()
        # Boot still went on: the new columns were added beside the old CHECK.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_message_relay_question_talk'").fetchone()
        conn.execute("UPDATE message_relays SET question_talk_id=7 WHERE id='r-waiting'")
        conn.execute("DELETE FROM message_relays WHERE request_id='orphan-request'")
    # Once the violation is gone the next boot rebuilds, keeping what was written.
    db.init_db(old)
    with db.get_db(old) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_relays'").fetchone()[0]
        assert "'room','whatsapp','sms'" in sql
        assert conn.execute(
            "SELECT question_talk_id FROM message_relays WHERE id='r-waiting'").fetchone()[0] == 7


def test_new_profile_and_attempt_columns(tmp_path, path):
    old = tmp_path / "old.db"
    schema = (Path(__file__).parents[1] / "schema.sql").read_text()
    for column in ("relay_delivery TEXT NOT NULL DEFAULT '',",
                   "attempt_tool_calls INTEGER NOT NULL DEFAULT 0,",
                   "attempt_first_tool_relay INTEGER NOT NULL DEFAULT 0,"):
        assert column in schema
        schema = schema.replace(column, "")
    with sqlite3.connect(old) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt) VALUES (1,'alice','web','x')")
        conn.execute("INSERT INTO user_profiles (user_id) VALUES ('alice')")
    db.init_db(old)
    for target in (path, old):
        with db.get_db(target) as conn:
            profile = {r[1]: tuple(r)[2:5] for r in conn.execute("PRAGMA table_info(user_profiles)")}
            tasks = {r[1]: tuple(r)[2:5] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert profile["relay_delivery"] == ("TEXT", 1, "''")
            assert tasks["attempt_tool_calls"] == ("INTEGER", 1, "0")
            assert tasks["attempt_first_tool_relay"] == ("INTEGER", 1, "0")
    with db.get_db(old) as conn:
        assert conn.execute("SELECT relay_delivery FROM user_profiles").fetchone()[0] == ""
        assert tuple(conn.execute(
            "SELECT attempt_tool_calls, attempt_first_tool_relay FROM tasks WHERE id=1").fetchone()) == (0, 0)
