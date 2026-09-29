"""Durable WhatsApp request identity, migration and ownership."""

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
    from istota.whatsapp_requests import _store_request
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
              "relay_permissions", "relay_reply_candidates")
    with db.get_db(path) as fresh, db.get_db(old) as upgraded:
        for table in tables:
            a = {r[1]: tuple(r)[2:5] for r in fresh.execute(f"PRAGMA table_info({table})")}
            b = {r[1]: tuple(r)[2:5] for r in upgraded.execute(f"PRAGMA table_info({table})")}
            assert a == b
        assert "whatsapp_confirmation_request_id" in {r[1] for r in upgraded.execute("PRAGMA table_info(tasks)")}
        assert upgraded.execute("SELECT prompt FROM tasks WHERE id=1").fetchone()[0] == "existing"
        assert upgraded.execute("SELECT name FROM sqlite_master WHERE name='idx_messages_delivery_reference'").fetchone()


def test_replay_scope_conflict_and_rollback(path):
    from istota.whatsapp_requests import RequestError, get_request
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
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        with pytest.raises(RequestError):
            store(conn, task(conn), request_key=key, text=text)


def test_task_deletion_and_cleanup_leave_identity(path):
    from istota.whatsapp_requests import cleanup_content, get_request
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
    from istota.whatsapp_requests import cleanup_content
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
