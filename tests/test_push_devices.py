"""Device ownership and watermark behavior through the real SQLite store."""

import sqlite3

import pytest

from istota import db
from istota.push import devices

TOKEN = "ab" * 32


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as connection:
        yield connection


def test_registration_moves_owner_resets_delivery_and_seeds_watermark(conn):
    room = db.register_room(conn, None, "alice", origin="web", name="Work")
    db.add_message(conn, room.token, role="assistant", body="Old reply", origin_surface="web")
    latest = db.room_max_message_id(conn, room.token)
    assert not devices.any_devices(conn)
    first = devices.register_device(conn, "alice", TOKEN, "sandbox", "ios", "0.11.0")
    assert first is not None
    assert devices.any_devices(conn)
    assert conn.execute("SELECT last_pushed_message_id FROM push_state WHERE user_id='alice'").fetchone()[0] == latest
    conn.execute("UPDATE push_devices SET last_badge=7, last_error='old', created_at='2000-01-01T00:00:00Z'")
    assert devices.register_device(conn, "bob", TOKEN, "sandbox", "ios", "0.11.1") == first
    assert devices.list_devices(conn, "alice") == []
    private, = devices.devices_for_user(conn, "bob")
    assert private["last_badge"] == -1
    assert private["last_error"] is None
    assert private["created_at"] != "2000-01-01T00:00:00Z"
    assert private["token"] == TOKEN
    public, = devices.list_devices(conn, "bob")
    assert "token" not in public and public["token_tail"] == TOKEN[-6:]
    assert devices.register_device(conn, "bob", TOKEN, "production") != first
    assert len(devices.list_devices(conn, "bob")) == 2
    assert not devices.delete_device(conn, first, "alice")
    assert devices.delete_device(conn, first, "bob")


def test_existing_watermark_is_preserved_and_failed_seed_rolls_back(conn):
    first = devices.register_device(conn, "alice", TOKEN, "sandbox")
    conn.execute("UPDATE push_state SET last_pushed_message_id=123 WHERE user_id='alice'")
    assert devices.register_device(conn, "alice", TOKEN, "sandbox") == first
    assert conn.execute("SELECT last_pushed_message_id FROM push_state").fetchone()[0] == 123
    conn.execute("DROP TABLE push_state")
    assert devices.register_device(conn, "bob", TOKEN, "sandbox") is None
    assert devices.devices_for_user(conn, "alice")[0]["id"] == first
    assert devices.devices_for_user(conn, "bob") == []


def test_unavailable_store_does_not_raise_or_log_token(caplog):
    conn = sqlite3.connect(":memory:")
    assert devices.register_device(conn, "alice", TOKEN, "sandbox") is None
    assert devices.list_devices(conn, "alice") == []
    assert devices.devices_for_user(conn, "alice") == []
    assert not devices.any_devices(conn)
    assert not devices.delete_device(conn, 1, "alice")
    assert TOKEN not in caplog.text
    conn.close()
