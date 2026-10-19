"""Mobile handoffs upgrade the framework DB without changing push enrollment."""

import sqlite3

import pytest

from istota import db


def test_mobile_schema_upgrade_retains_push_rows(tmp_path):
    path = tmp_path / "upgrade.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        conn.execute("DROP TABLE IF EXISTS web_mobile_auth_requests")
        conn.execute("INSERT INTO push_devices(user_id,token,environment) VALUES('alice',?,'sandbox')", ("ab" * 32,))
    for _ in range(2):
        db.init_db(path)
    with db.get_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM web_mobile_auth_requests").fetchone()[0] == 0
        assert tuple(conn.execute("SELECT user_id,token,environment FROM push_devices").fetchone()) == ("alice", "ab" * 32, "sandbox")
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(web_mobile_auth_requests)")}
        assert {"idx_web_mobile_auth_expiry", "idx_web_mobile_auth_code", "idx_web_mobile_auth_source"} <= indexes


def test_mobile_migration_joins_callers_transaction(db_path):
    with db.get_db(db_path) as conn:
        conn.execute("INSERT INTO push_state(user_id) VALUES('alice')")
        db._migrate_mobile_auth_requests(conn)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM push_state").fetchone()[0] == 0
        conn.execute("INSERT INTO web_mobile_auth_requests(request_id,challenge,state,source,created_at,expires_at,code_digest) VALUES('one','c','s','ip','now','later','digest')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO web_mobile_auth_requests(request_id,challenge,state,source,created_at,expires_at,code_digest) VALUES('two','c','s','ip','now','later','digest')")
