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
