"""Fresh and upgraded push schemas preserve rows and default to no backlog."""

import pytest

from istota import db, user_profiles


@pytest.mark.parametrize("upgrade", [False, True])
def test_push_schema_is_idempotent(tmp_path, upgrade):
    path = tmp_path / "test.db"
    db.init_db(path)
    user_profiles.ensure_profile(path, "alice", display_name="Alice")
    with db.get_db(path) as conn:
        conn.execute("INSERT INTO notifications (user_id, source, dedup_key, title) VALUES ('alice', 'test', 'one', 'Keep me')")
        if upgrade:
            conn.execute("DROP TABLE push_devices")
            conn.execute("DROP TABLE push_state")
            conn.execute("ALTER TABLE notifications DROP COLUMN push_pending")
            conn.execute("ALTER TABLE user_profiles DROP COLUMN push_chat_alerts")
            conn.execute("ALTER TABLE user_profiles DROP COLUMN push_preview")
    for _ in range(2):
        db.init_db(path)
    with db.get_db(path) as conn:
        row = conn.execute("SELECT title, push_pending FROM notifications").fetchone()
        assert tuple(row) == ("Keep me", 0)
        profile = user_profiles.get_profile(path, "alice", conn=conn)
        assert profile.display_name == "Alice"
        assert profile.push_chat_alerts == "alerts"
        assert profile.push_preview is True
        for _ in range(2):
            db._migrate_push_tables(conn)
            db._migrate_notifications_push_pending(conn)
        assert conn.execute("SELECT COUNT(*) FROM push_state").fetchone()[0] == 0
        conn.execute("INSERT INTO push_devices (user_id, token, environment) VALUES ('alice', ?, 'sandbox')", ("ab" * 32,))
        assert conn.execute("SELECT last_badge FROM push_devices").fetchone()[0] == -1


def test_migrations_do_not_commit_callers_transaction(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute("INSERT INTO push_state(user_id) VALUES ('alice')")
        db._migrate_push_tables(conn)
        db._migrate_notifications_push_pending(conn)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM push_state").fetchone()[0] == 0


def test_preference_upgrade_preserves_profiles_rooms_and_devices(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    user_profiles.ensure_profile(path, "alice")
    with db.get_db(path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Keep room")
        conn.execute("INSERT INTO push_devices(user_id,token,environment) VALUES('alice',?,'sandbox')", ("ab" * 32,))
        for table, column in (("user_profiles", "push_inbox_categories"), ("web_chat_rooms", "push_notifications"),
                              ("push_devices", "auth_method"), ("push_devices", "auth_epoch"),
                              ("push_devices", "auth_session_id"), ("push_devices", "connection_binding"),
                              ("push_devices", "connection_generation")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for _ in range(2):
        db.init_db(path)
    with db.get_db(path) as conn:
        assert user_profiles.get_profile(path, "alice", conn=conn).push_inbox_categories == {}
        restored = db.get_web_chat_room(conn, room.id)
        assert restored.name == "Keep room" and restored.push_notifications == "default"
        row = conn.execute("SELECT auth_method,auth_epoch,auth_session_id,connection_binding,connection_generation,"
                           "token FROM push_devices").fetchone()
        assert tuple(row) == (None, None, None, None, None, "ab" * 32)


def test_legacy_single_handle_rebuild_preserves_override(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE web_chat_rooms (
        id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        push_notifications TEXT NOT NULL DEFAULT 'default')""")
    conn.execute("INSERT INTO web_chat_rooms VALUES(1,'alice','room','Keep',0,'then','then','badge')")
    db._migrate_web_chat_rooms_peruser(conn)
    db._migrate_web_chat_rooms_peruser(conn)
    assert conn.execute("SELECT push_notifications FROM web_chat_rooms").fetchone()[0] == "badge"
    conn.execute("INSERT INTO web_chat_rooms(user_id,token,name,created_at,updated_at) VALUES('bob','room','Keep','then','then')")
    assert conn.execute("SELECT push_notifications FROM web_chat_rooms WHERE user_id='bob'").fetchone()[0] == "default"
    conn.close()
