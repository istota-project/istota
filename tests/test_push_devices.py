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


ENCRYPTED = {
    "private_version": 1, "private_content": "encrypted",
    "encryption_public_key": "k" * 43, "encryption_key_id": "e" * 43,
    "sender_key_id": "s" * 43, "push_generation": "g" * 22,
}


def _private_row(conn):
    return tuple(conn.execute(
        "SELECT private_version, private_content, encryption_public_key, encryption_key_id, sender_key_id,"
        " push_generation FROM push_devices").fetchone())


def test_private_enrollment_is_stored_and_plain_reregistration_clears_it(conn):
    binding = {"connection_binding": "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51", "connection_generation": 1}
    device_id = devices.register_device(conn, "alice", TOKEN, "sandbox", **binding, **ENCRYPTED)
    assert _private_row(conn) == tuple(ENCRYPTED.values())
    private, = devices.devices_for_user(conn, "alice")
    assert private["encryption_public_key"] == "k" * 43
    assert devices.register_device(conn, "alice", TOKEN, "sandbox", **binding) == device_id
    assert _private_row(conn) == (None,) * 6


@pytest.mark.parametrize("installation,enrollment,sender,expected", [
    ("legacy", ENCRYPTED, "s" * 43, ("legacy", False)),
    ("private", ENCRYPTED, "s" * 43, ("encrypted", False)),
    ("private", ENCRYPTED, "x" * 43, ("generic", True)),
    ("private", ENCRYPTED, None, ("encrypted", False)),
    ("private", {"private_version": 1, "private_content": "generic", "push_generation": "g" * 22},
     "s" * 43, ("generic", False)),
    ("private", {}, "s" * 43, ("generic", False)),
])
def test_device_list_reports_effective_content(conn, installation, enrollment, sender, expected):
    devices.register_device(conn, "alice", TOKEN, "sandbox", **enrollment)
    listed, = devices.list_devices(conn, "alice", content_mode=installation, sender_key_id=sender)
    assert (listed["content"], listed["stale_key"]) == expected
    for secret in ("k" * 43, "e" * 43, "s" * 43, "g" * 22):
        assert secret not in str(listed)
    assert not set(listed) & {"encryption_public_key", "encryption_key_id", "sender_key_id", "push_generation"}


def test_registration_generation_changes_with_same_clock(conn, monkeypatch):
    monkeypatch.setattr(devices, "iso_utc_now", lambda: "2026-01-01T00:00:00.000Z")
    device_id = devices.register_device(conn, "alice", TOKEN, "sandbox")
    first = devices.devices_for_user(conn, "alice")[0]["created_at"]
    assert devices.register_device(conn, "alice", TOKEN, "sandbox") == device_id
    second = devices.devices_for_user(conn, "alice")[0]["created_at"]
    assert second > first
    assert not devices.delete_device(conn, device_id, "alice", created_at=first)
    assert devices.delete_device(conn, device_id, "alice", created_at=second)
