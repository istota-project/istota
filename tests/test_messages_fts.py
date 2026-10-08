"""Transcript index upgrades, write hooks and the aggregate visibility seam."""

import re
import sqlite3
from pathlib import Path

import pytest

from istota import db


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return path


def room(conn, token="room", user="alice"):
    conn.execute("INSERT INTO rooms (token,user_id,name,origin) VALUES (?, ?, ?, 'web')", (token, user, token))
    conn.execute("INSERT INTO room_members (room_token,user_id) VALUES (?, ?)", (token, user))


def message(conn, token="room", body="falcon", **kwargs):
    return db.add_message(conn, token, role=kwargs.pop("role", "user"), body=body,
                          origin_surface=kwargs.pop("origin_surface", "web"), **kwargs)


def matches(conn, query="falcon"):
    return [r[0] for r in conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rowid", (query,),
    )]


def test_insert_update_delete_and_cascade(path):
    with db.get_db(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        room(conn)
        ident = message(conn)
        assert matches(conn) == [ident]
        conn.execute("UPDATE messages SET body='kestrel', title='falcon' WHERE id=?", (ident,))
        assert matches(conn, "kestrel") == [ident]
        assert matches(conn, "falcon") == [ident]
        conn.execute("UPDATE messages SET title=NULL WHERE id=?", (ident,))
        assert matches(conn, "falcon") == []
        conn.execute("DELETE FROM messages WHERE id=?", (ident,))
        assert matches(conn, "kestrel") == []
        message(conn)
        conn.execute("DELETE FROM rooms WHERE token='room'")
        assert matches(conn) == []
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_actual_room_delete_cleans_index_without_foreign_keys(path):
    with db.get_db(path) as conn:
        room(conn)
        message(conn)
        assert matches(conn)
        db._delete_room_rows(conn, "room")
        assert matches(conn) == []


def old_schema():
    schema = (Path(__file__).parents[1] / "schema.sql").read_text()
    schema = re.sub(r"CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts .*?;", "", schema, flags=re.S)
    return re.sub(r"CREATE TRIGGER IF NOT EXISTS messages_fts_\w+ .*?END;", "", schema, flags=re.S)


def shape(conn):
    return [(r[0], r[1], re.sub(r"\s+", " ", r[2]).strip()) for r in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name GLOB 'messages_fts*' AND sql IS NOT NULL ORDER BY name",
    )]


def test_upgrade_indexes_existing_rows_once_and_matches_fresh_schema(tmp_path, path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.executescript(old_schema())
        room(conn)
        conn.execute("INSERT INTO messages (room_token,role,body,origin_surface) VALUES ('room','user','falcon','web')")
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone()
    db.init_db(old)
    with db.get_db(old) as conn, db.get_db(path) as fresh:
        assert matches(conn) == [1]
        assert shape(conn) == shape(fresh)
        statements = []
        conn.set_trace_callback(statements.append)
        db._migrate_messages_fts(conn)
        assert not any("rebuild" in sql.lower() for sql in statements)
    db.init_db(old)
    with db.get_db(old) as conn:
        assert matches(conn) == [1]
        message(conn, body="falcon new")
        assert len(matches(conn)) == 2


def test_search_uses_all_view_scope_and_fresh_membership(path):
    with db.get_db(path) as conn:
        for token in ("shared", "dismissed", "archived", "hidden"):
            room(conn, token)
        room(conn, "other", "bob")
        conn.execute("INSERT INTO room_members (room_token,user_id) VALUES ('shared','bob')")
        conn.execute("INSERT INTO room_dismissals (room_token,user_id) VALUES ('dismissed','alice')")
        conn.execute("UPDATE rooms SET archived=1 WHERE token='archived'")
        conn.execute("INSERT INTO room_bindings (room_token,surface,surface_ref) VALUES ('hidden','email','thread@example.com')")
        shared = message(conn, "shared", author_user_id="bob")
        system = message(conn, "shared", role="system", origin_surface="scheduled")
        message(conn, "shared", origin_surface="scheduled")
        for token in ("dismissed", "archived", "hidden", "other"):
            message(conn, token)
        exclude = db.hidden_room_tokens_for_member(conn, "alice")
        assert exclude == {"hidden"}
        hits = db.search_messages(conn, "alice", '"falcon"*', limit=20, offset=0, exclude_tokens=exclude)
        assert {h["msg_id"] for h in hits} == {shared, system}
        assert {h["msg_id"] for h in hits} == {
            r["msg_id"] for r in db.list_messages_across_rooms(conn, "alice", exclude_tokens=exclude)
        }
        assert hits[0]["shared"] == 1
        assert db.search_messages(conn, "outsider", '"falcon"*', limit=20, offset=0, exclude_tokens=set()) == []
        conn.execute("DELETE FROM room_members WHERE user_id='alice' AND room_token='shared'")
        assert db.search_messages(conn, "alice", '"falcon"*', limit=20, offset=0, exclude_tokens=exclude) == []


def test_rank_recency_paging_and_title_snippet(path):
    with db.get_db(path) as conn:
        room(conn)
        a = message(conn, body="falcon")
        b = message(conn, body="falcon")
        c = message(conn, body="other words", title="falcon", role="system")
        conn.execute("UPDATE messages SET created_at='2025-01-01 00:00:00'")
        conn.execute("UPDATE messages SET created_at='2025-02-01 00:00:00' WHERE id=?", (a,))
        conn.execute("INSERT INTO message_stars (message_id,user_id) VALUES (?, 'alice')", (a,))
        hits = db.search_messages(conn, "alice", '"falcon"*', limit=20, offset=0, exclude_tokens=set())
        assert [h["msg_id"] for h in hits] == [a, b, c]
        assert hits[0]["starred"] == 1
        assert hits[2]["snippet"] == "falcon"
        assert hits[2]["highlights"] == [[0, 6]]
        assert [h["msg_id"] for h in db.search_messages(conn, "alice", '"falcon"*', limit=1, offset=1, exclude_tokens=set())] == [b]
        assert db.search_messages(conn, "alice", '"falcon"*', limit=1, offset=10, exclude_tokens=set()) == []


def test_failed_rebuild_rolls_back_table_and_triggers(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.executescript(old_schema())
        room(conn)
        conn.execute("INSERT INTO messages (room_token,role,body,origin_surface) VALUES ('room','user','falcon','web')")
        conn.commit()
        # Refuse the rebuild's scan, after its DDL has run.
        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == "messages" and column == "body":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            db._migrate_messages_fts(conn)
        conn.set_authorizer(None)
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'messages_fts*'").fetchall()
        db._migrate_messages_fts(conn)
        assert matches(conn) == [1]


def test_search_name_fallback_and_equal_rank_id_order(path):
    with db.get_db(path) as conn:
        room(conn)
        conn.execute("INSERT INTO web_chat_rooms (user_id,token,name) VALUES ('alice','room','Handle')")
        a = message(conn)
        b = message(conn)
        conn.execute("UPDATE messages SET created_at='2025-01-01 00:00:00'")
        hits = db.search_messages(conn, "alice", '"falcon"', limit=10, offset=0, exclude_tokens=set())
        assert [h["msg_id"] for h in hits] == [b, a]
        assert hits[0]["room_name"] == "room"
        conn.execute("UPDATE rooms SET name=NULL WHERE token='room'")
        hits = db.search_messages(conn, "alice", '"falcon"', limit=10, offset=0, exclude_tokens=set())
        assert hits[0]["room_name"] == "Handle"
