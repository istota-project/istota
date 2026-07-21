"""Tests for the shared (cross-user) KV store DB functions."""

from istota import db


class TestSharedKvSet:
    def test_set_new_key(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "greeting", '"hello"', "admin")
        row = db.shared_kv_get(db_conn, "ns", "greeting")
        assert row is not None
        assert row["value"] == '"hello"'
        assert row["written_by"] == "admin"

    def test_set_upserts_and_records_writer(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "count", "1", "alice")
        db.shared_kv_set(db_conn, "ns", "count", "2", "bob")
        row = db.shared_kv_get(db_conn, "ns", "count")
        assert row["value"] == "2"
        assert row["written_by"] == "bob"  # last writer wins

    def test_set_different_namespaces_independent(self, db_conn):
        db.shared_kv_set(db_conn, "ns1", "key", '"a"', "admin")
        db.shared_kv_set(db_conn, "ns2", "key", '"b"', "admin")
        assert db.shared_kv_get(db_conn, "ns1", "key")["value"] == '"a"'
        assert db.shared_kv_get(db_conn, "ns2", "key")["value"] == '"b"'


class TestSharedKvGet:
    def test_get_missing_returns_none(self, db_conn):
        assert db.shared_kv_get(db_conn, "ns", "nope") is None

    def test_get_returns_updated_at(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "k", '"v"', "admin")
        row = db.shared_kv_get(db_conn, "ns", "k")
        assert row["updated_at"]


class TestSharedKvDelete:
    def test_delete_existing_returns_true(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "k", '"v"', "admin")
        assert db.shared_kv_delete(db_conn, "ns", "k") is True
        assert db.shared_kv_get(db_conn, "ns", "k") is None

    def test_delete_missing_returns_false(self, db_conn):
        assert db.shared_kv_delete(db_conn, "ns", "nope") is False


class TestSharedKvListNamespaces:
    def test_list_ordered_by_key(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "b", '"2"', "admin")
        db.shared_kv_set(db_conn, "ns", "a", '"1"', "admin")
        entries = db.shared_kv_list(db_conn, "ns")
        assert [e["key"] for e in entries] == ["a", "b"]
        assert entries[0]["written_by"] == "admin"

    def test_list_empty_namespace(self, db_conn):
        assert db.shared_kv_list(db_conn, "empty") == []

    def test_namespaces_distinct_ordered(self, db_conn):
        db.shared_kv_set(db_conn, "zeta", "k", "1", "admin")
        db.shared_kv_set(db_conn, "alpha", "k", "1", "admin")
        db.shared_kv_set(db_conn, "alpha", "k2", "1", "admin")
        assert db.shared_kv_namespaces(db_conn) == ["alpha", "zeta"]


class TestTableIsolation:
    def test_shared_write_does_not_touch_istota_kv(self, db_conn):
        db.shared_kv_set(db_conn, "ns", "k", '"shared"', "admin")
        # A per-user read of the same ns/key finds nothing — separate tables.
        assert db.kv_get(db_conn, "admin", "ns", "k") is None

    def test_peruser_write_does_not_touch_shared_kv(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "k", '"personal"')
        assert db.shared_kv_get(db_conn, "ns", "k") is None
