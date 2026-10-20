"""Per-device location ingest tokens through the real SQLite store."""

import hashlib

import pytest

from istota import db
from istota.location import ingest_tokens

DEVICE = "6F1C2A3B-4D5E-4F60-8A7B-9C0D1E2F3A4B"
OTHER_DEVICE = "0A1B2C3D-4E5F-4061-8273-94A5B6C7D8E9"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as connection:
        yield connection


def _row(conn, row_id):
    return dict(conn.execute("SELECT * FROM location_ingest_tokens WHERE id = ?", (row_id,)).fetchone())


def test_hash_token_is_sha256_hex():
    assert ingest_tokens.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_mint_returns_token_once_and_stores_only_hash_and_tail(conn):
    row_id, token = ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    assert len(token) == 43
    row = _row(conn, row_id)
    assert row["token_hash"] == ingest_tokens.hash_token(token)
    assert row["token_tail"] == token[-4:]
    assert row["kind"] == "code" and row["device_id"] is None
    assert row["label"] == "QR code"
    assert row["created_at"].endswith("Z") and row["last_used_at"] is None
    assert token not in {str(value) for value in row.values()}


def test_app_mint_for_same_device_replaces_in_place(conn):
    first_id, first_token = ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    conn.execute(
        "UPDATE location_ingest_tokens SET created_at = '2000-01-01T00:00:00.000Z', last_used_at = 'x' WHERE id = ?",
        (first_id,),
    )
    second_id, second_token = ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    assert second_id == first_id
    assert second_token != first_token
    row = _row(conn, first_id)
    assert row["token_hash"] == ingest_tokens.hash_token(second_token)
    assert row["token_tail"] == second_token[-4:]
    assert row["created_at"] != "2000-01-01T00:00:00.000Z"
    assert row["last_used_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM location_ingest_tokens").fetchone()[0] == 1


def test_same_device_under_two_users_gets_two_rows(conn):
    alice_id, _ = ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    bob_id, _ = ingest_tokens.mint(conn, "bob", "app", DEVICE, "iPhone")
    assert alice_id != bob_id


def test_code_mint_always_inserts(conn):
    first_id, _ = ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    second_id, _ = ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    assert first_id != second_id
    assert len(ingest_tokens.list_tokens(conn, "alice")) == 2


def test_cap_refuses_a_new_row_but_not_a_replace(conn):
    ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    for _ in range(ingest_tokens.MAX_TOKENS_PER_USER - 1):
        ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    with pytest.raises(ingest_tokens.TooManyDevices):
        ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    with pytest.raises(ingest_tokens.TooManyDevices):
        ingest_tokens.mint(conn, "alice", "app", OTHER_DEVICE, "iPhone")
    assert len(ingest_tokens.list_tokens(conn, "alice")) == ingest_tokens.MAX_TOKENS_PER_USER
    ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    assert len(ingest_tokens.list_tokens(conn, "alice")) == ingest_tokens.MAX_TOKENS_PER_USER
    ingest_tokens.mint(conn, "bob", "code", None, "QR code")


def test_mint_rejects_a_kind_and_device_id_that_do_not_fit(conn):
    with pytest.raises(ValueError):
        ingest_tokens.mint(conn, "alice", "phone", None, "iPhone")
    with pytest.raises(ValueError):
        ingest_tokens.mint(conn, "alice", "app", None, "iPhone")
    with pytest.raises(ValueError):
        ingest_tokens.mint(conn, "alice", "code", DEVICE, "QR code")
    assert ingest_tokens.list_tokens(conn, "alice") == []


def test_list_never_returns_the_hash_and_is_newest_first(conn):
    old_id, _ = ingest_tokens.mint(conn, "alice", "code", None, "Old")
    conn.execute(
        "UPDATE location_ingest_tokens SET created_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (old_id,),
    )
    new_id, token = ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    ingest_tokens.mint(conn, "bob", "code", None, "Bob's")
    rows = ingest_tokens.list_tokens(conn, "alice")
    assert [row["id"] for row in rows] == [new_id, old_id]
    assert set(rows[0]) == {"id", "kind", "device_id", "label", "token_tail", "created_at", "last_used_at"}
    assert rows[0]["token_tail"] == token[-4:]
    assert rows[0]["device_id"] == DEVICE


def test_delete_refuses_another_users_row(conn):
    row_id, _ = ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    assert not ingest_tokens.delete_token(conn, "bob", row_id)
    assert len(ingest_tokens.list_tokens(conn, "alice")) == 1
    assert ingest_tokens.delete_token(conn, "alice", row_id)
    assert ingest_tokens.list_tokens(conn, "alice") == []
    assert not ingest_tokens.delete_token(conn, "alice", row_id)


def test_all_token_hashes_covers_only_the_named_users(conn):
    alice_id, alice_token = ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    bob_id, bob_token = ingest_tokens.mint(conn, "bob", "code", None, "QR code")
    ingest_tokens.mint(conn, "carol", "code", None, "QR code")
    assert sorted(ingest_tokens.all_token_hashes(conn, ["alice", "bob"])) == sorted([
        (ingest_tokens.hash_token(alice_token), "alice", alice_id),
        (ingest_tokens.hash_token(bob_token), "bob", bob_id),
    ])
    assert ingest_tokens.all_token_hashes(conn, []) == []


def test_touch_last_used(conn):
    row_id, _ = ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    ingest_tokens.touch_last_used(conn, row_id, "2026-10-19T12:00:00.000Z")
    assert ingest_tokens.list_tokens(conn, "alice")[0]["last_used_at"] == "2026-10-19T12:00:00.000Z"


def test_failed_mint_does_not_roll_back_the_callers_transaction(conn):
    conn.execute("INSERT INTO push_state(user_id) VALUES ('alice')")
    for _ in range(ingest_tokens.MAX_TOKENS_PER_USER):
        ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    with pytest.raises(ingest_tokens.TooManyDevices):
        ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    assert conn.execute("SELECT COUNT(*) FROM push_state").fetchone()[0] == 1


def _indexes(conn):
    return {row[1] for row in conn.execute("PRAGMA index_list(location_ingest_tokens)")}


def test_migration_creates_the_table_on_an_existing_database_and_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute("DROP TABLE location_ingest_tokens")
        db._migrate_location_ingest_tokens(conn)
        db._migrate_location_ingest_tokens(conn)
        assert {"idx_location_ingest_tokens_device", "idx_location_ingest_tokens_user"} <= _indexes(conn)
        ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
    db.init_db(path)
    with db.get_db(path) as conn:
        assert len(ingest_tokens.list_tokens(conn, "alice")) == 1
        conn.execute("DROP TABLE location_ingest_tokens")
    db.init_db(path)
    with db.get_db(path) as conn:
        assert {"idx_location_ingest_tokens_device", "idx_location_ingest_tokens_user"} <= _indexes(conn)
        assert ingest_tokens.list_tokens(conn, "alice") == []


def test_migration_does_not_commit_the_callers_transaction(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute("INSERT INTO push_state(user_id) VALUES ('alice')")
        db._migrate_location_ingest_tokens(conn)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM push_state").fetchone()[0] == 0


def test_kind_check_constraint(conn):
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO location_ingest_tokens (user_id, token_hash, token_tail, kind, label, created_at)"
            " VALUES ('alice', 'h', 't', 'other', 'x', 'now')"
        )
