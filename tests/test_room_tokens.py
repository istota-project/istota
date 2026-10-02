"""Minted room identity and the migration inventory's schema contract."""

import base64
import re
import sqlite3
from pathlib import Path

import pytest

from istota import db

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


def test_mint_room_token_format_and_entropy():
    tokens = {db.mint_room_token() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert re.fullmatch(r"rm_[A-Za-z0-9_-]{22}", token)
        assert len(base64.urlsafe_b64decode(token[3:] + "==")) == 16
        assert db.is_canonical_room_token(token)


@pytest.mark.parametrize("value", [
    None, 12, False, b"rm_example", [], {}, object(), "", "talkref",
    "web-example", "email-thread-example", "sms-example", "whatsapp-example",
    "RM_example", "prefix_rm_example",
])
def test_room_token_recognition_is_total(value):
    assert db.is_canonical_room_token(value) is False


def test_room_token_recognition_names_the_namespace():
    # Recognition is a prefix check, not a parser or an existence lookup.
    assert db.is_canonical_room_token("rm_example") is True


def test_mapping_survives_database_reinitialization(tmp_path):
    path = tmp_path / "rooms.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        columns = {row["name"]: row for row in conn.execute(
            "PRAGMA table_info(room_token_migration)",
        )}
        assert set(columns) == {"old_token", "new_token", "migrated_at"}
        assert columns["old_token"]["pk"] == 1
        assert columns["new_token"]["notnull"] == 1
        assert columns["migrated_at"]["notnull"] == 1
        conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)",
                     ("old-room", "rm_example", "2026-01-01T00:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)",
                         ("old-room", "rm_other", "2026-01-01T00:00:00Z"))
    db.init_db(path)
    with db.get_db(path) as conn:
        assert tuple(conn.execute("SELECT * FROM room_token_migration").fetchone()) == (
            "old-room", "rm_example", "2026-01-01T00:00:00Z",
        )


# Names cannot reveal tokens inside JSON or a memory namespace. Keep those
# audited holders explicit too, and verify every listed column still exists.
EMBEDDED_HOLDERS = {
    ("istota_kv", "value"),
    ("memory_chunks", "user_id"),
    ("memory_chunks", "source_id"),
    ("memory_chunks", "metadata_json"),
    ("message_relays", "origin"),
    ("message_relays", "destination"),
    ("whatsapp_skill_requests", "origin"),
    ("whatsapp_skill_requests", "destination"),
}
REFERENCE_NAMES = {
    "surface_ref", "side_of", "default_room", "thread_id", "output_target",
    "origin_target", "routing", "default_destination", "log_channel",
    "alerts_channel", "binding_fingerprint",
}


def _schema_columns(conn):
    columns = set()
    candidates = set(EMBEDDED_HOLDERS)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        quoted_table = '\"' + table.replace('\"', '\"\"') + '\"'
        for _, name, _, *_ in conn.execute(f"PRAGMA table_info({quoted_table})"):
            column = (table, name)
            columns.add(column)
            if "token" in name or name in REFERENCE_NAMES:
                candidates.add(column)
        for row in conn.execute(f"PRAGMA foreign_key_list({quoted_table})"):
            if row[2] == "rooms":
                candidates.add((table, row[3]))
    return columns, candidates


def _assert_inventory(conn):
    from istota.maintenance.room_relocate import PRESERVE_COLUMNS, REWRITE_COLUMNS

    rewrite, preserve = set(REWRITE_COLUMNS), set(PRESERVE_COLUMNS)
    columns, candidates = _schema_columns(conn)
    assert not rewrite & preserve, f"multiply classified: {sorted(rewrite & preserve)}"
    assert not candidates - (rewrite | preserve), (
        f"unclassified: {sorted(candidates - (rewrite | preserve))}"
    )
    assert not (rewrite | preserve) - columns, (
        f"stale inventory: {sorted((rewrite | preserve) - columns)}"
    )
    assert all(REWRITE_COLUMNS.values()) and all(PRESERVE_COLUMNS.values())


@pytest.mark.parametrize("initialized", [False, True], ids=["schema", "init_db"])
def test_token_columns_have_exactly_one_disposition(tmp_path, initialized):
    path = tmp_path / "inventory.db"
    if initialized:
        db.init_db(path)
    with sqlite3.connect(path) as conn:
        if not initialized:
            conn.executescript(SCHEMA.read_text())
        _assert_inventory(conn)


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE rooms ADD COLUMN future_token TEXT",
    "ALTER TABLE rooms ADD COLUMN future_token VARCHAR(255)",
    "ALTER TABLE rooms ADD COLUMN future_token",
    "CREATE TABLE future_room_holder (parent TEXT REFERENCES rooms(token))",
])
def test_inventory_guard_refuses_an_unclassified_holder(ddl):
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(SCHEMA.read_text())
        conn.execute(ddl)
        with pytest.raises(AssertionError, match="unclassified:.*future"):
            _assert_inventory(conn)


def test_mixed_and_structured_holders_require_specific_handlers():
    from istota.maintenance.room_relocate import REWRITE_COLUMNS

    assert REWRITE_COLUMNS["sent_emails", "conversation_token"] == "email_thread"
    assert REWRITE_COLUMNS["processed_emails", "thread_id"] == "email_thread"
    assert REWRITE_COLUMNS["message_relays", "destination"] == "destination_json"
    assert REWRITE_COLUMNS["message_relays", "origin"] == "origin_json"
    assert REWRITE_COLUMNS["message_relays", "binding_fingerprint"] == "destination_fingerprint"


@pytest.mark.parametrize('upgrade', [False, True])
def test_binding_reference_is_unique(tmp_path, upgrade):
    import sqlite3
    path = tmp_path / 'bindings.db'
    db.init_db(path)
    if upgrade:
        with db.get_db(path) as conn:
            conn.execute('DROP INDEX IF EXISTS idx_room_bindings_unique_ref')
        db.init_db(path)
    with db.get_db(path) as conn:
        first = db.register_room(conn, None, 'alice', origin='sms').token
        second = db.register_room(conn, None, 'alice', origin='sms').token
        conn.execute("INSERT INTO room_bindings (room_token, surface, surface_ref) VALUES (?, 'sms', 'same')", (first,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO room_bindings (room_token, surface, surface_ref) VALUES (?, 'sms', 'same')", (second,))


def test_binding_upgrade_refuses_ambiguity_without_changes(tmp_path):
    import sqlite3
    path = tmp_path / 'ambiguous.db'
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute('DROP INDEX IF EXISTS idx_room_bindings_unique_ref')
        for token in ('one', 'two'):
            db.register_room(conn, token, 'alice', origin='talk')
            conn.execute("INSERT INTO room_bindings (room_token, surface, surface_ref) VALUES (?, 'talk', 'same')", (token,))
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match='ambiguous room bindings.*resolve'):
        db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before
