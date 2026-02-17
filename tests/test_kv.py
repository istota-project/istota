"""Tests for istota_kv key-value store: DB functions and CLI commands."""

import json
from contextlib import contextmanager

from istota import db
from istota.cli import cmd_kv_get, cmd_kv_set, cmd_kv_list, cmd_kv_delete, cmd_kv_namespaces


# ============================================================================
# DB Functions
# ============================================================================


class TestKvSet:
    def test_set_new_key(self, db_conn):
        db.kv_set(db_conn, "alice", "test_ns", "greeting", '"hello"')
        result = db.kv_get(db_conn, "alice", "test_ns", "greeting")
        assert result is not None
        assert result["value"] == '"hello"'

    def test_set_upserts_existing_key(self, db_conn):
        db.kv_set(db_conn, "alice", "test_ns", "count", "1")
        db.kv_set(db_conn, "alice", "test_ns", "count", "2")
        result = db.kv_get(db_conn, "alice", "test_ns", "count")
        assert result["value"] == "2"

    def test_set_updates_timestamp_on_upsert(self, db_conn):
        db.kv_set(db_conn, "alice", "test_ns", "key1", '"v1"')
        row1 = db.kv_get(db_conn, "alice", "test_ns", "key1")
        db.kv_set(db_conn, "alice", "test_ns", "key1", '"v2"')
        row2 = db.kv_get(db_conn, "alice", "test_ns", "key1")
        assert row2["updated_at"] >= row1["updated_at"]

    def test_set_different_namespaces_independent(self, db_conn):
        db.kv_set(db_conn, "alice", "ns1", "key", '"a"')
        db.kv_set(db_conn, "alice", "ns2", "key", '"b"')
        assert db.kv_get(db_conn, "alice", "ns1", "key")["value"] == '"a"'
        assert db.kv_get(db_conn, "alice", "ns2", "key")["value"] == '"b"'

    def test_set_different_users_independent(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"a"')
        db.kv_set(db_conn, "bob", "ns", "key", '"b"')
        assert db.kv_get(db_conn, "alice", "ns", "key")["value"] == '"a"'
        assert db.kv_get(db_conn, "bob", "ns", "key")["value"] == '"b"'

    def test_set_json_object_value(self, db_conn):
        value = json.dumps({"count": 42, "items": [1, 2, 3]})
        db.kv_set(db_conn, "alice", "ns", "data", value)
        result = db.kv_get(db_conn, "alice", "ns", "data")
        assert json.loads(result["value"]) == {"count": 42, "items": [1, 2, 3]}


class TestKvGet:
    def test_get_existing_key(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"value"')
        result = db.kv_get(db_conn, "alice", "ns", "key")
        assert result is not None
        assert result["value"] == '"value"'
        assert "updated_at" in result

    def test_get_nonexistent_key_returns_none(self, db_conn):
        result = db.kv_get(db_conn, "alice", "ns", "missing")
        assert result is None

    def test_get_wrong_user_returns_none(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"value"')
        result = db.kv_get(db_conn, "bob", "ns", "key")
        assert result is None

    def test_get_wrong_namespace_returns_none(self, db_conn):
        db.kv_set(db_conn, "alice", "ns1", "key", '"value"')
        result = db.kv_get(db_conn, "alice", "ns2", "key")
        assert result is None


class TestKvDelete:
    def test_delete_existing_key(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"value"')
        deleted = db.kv_delete(db_conn, "alice", "ns", "key")
        assert deleted is True
        assert db.kv_get(db_conn, "alice", "ns", "key") is None

    def test_delete_nonexistent_key(self, db_conn):
        deleted = db.kv_delete(db_conn, "alice", "ns", "missing")
        assert deleted is False

    def test_delete_scoped_to_user(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"value"')
        deleted = db.kv_delete(db_conn, "bob", "ns", "key")
        assert deleted is False
        assert db.kv_get(db_conn, "alice", "ns", "key") is not None


class TestKvList:
    def test_list_empty_namespace(self, db_conn):
        entries = db.kv_list(db_conn, "alice", "empty_ns")
        assert entries == []

    def test_list_returns_all_keys_in_namespace(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "a", '"1"')
        db.kv_set(db_conn, "alice", "ns", "b", '"2"')
        db.kv_set(db_conn, "alice", "ns", "c", '"3"')
        entries = db.kv_list(db_conn, "alice", "ns")
        assert len(entries) == 3
        keys = [e["key"] for e in entries]
        assert sorted(keys) == ["a", "b", "c"]

    def test_list_scoped_to_user(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "a", '"1"')
        db.kv_set(db_conn, "bob", "ns", "b", '"2"')
        entries = db.kv_list(db_conn, "alice", "ns")
        assert len(entries) == 1
        assert entries[0]["key"] == "a"

    def test_list_scoped_to_namespace(self, db_conn):
        db.kv_set(db_conn, "alice", "ns1", "a", '"1"')
        db.kv_set(db_conn, "alice", "ns2", "b", '"2"')
        entries = db.kv_list(db_conn, "alice", "ns1")
        assert len(entries) == 1
        assert entries[0]["key"] == "a"

    def test_list_entries_have_expected_fields(self, db_conn):
        db.kv_set(db_conn, "alice", "ns", "key", '"val"')
        entries = db.kv_list(db_conn, "alice", "ns")
        assert len(entries) == 1
        entry = entries[0]
        assert "key" in entry
        assert "value" in entry
        assert "updated_at" in entry


class TestKvNamespaces:
    def test_namespaces_empty(self, db_conn):
        ns = db.kv_namespaces(db_conn, "alice")
        assert ns == []

    def test_namespaces_returns_distinct(self, db_conn):
        db.kv_set(db_conn, "alice", "ns1", "a", '"1"')
        db.kv_set(db_conn, "alice", "ns1", "b", '"2"')
        db.kv_set(db_conn, "alice", "ns2", "c", '"3"')
        ns = db.kv_namespaces(db_conn, "alice")
        assert sorted(ns) == ["ns1", "ns2"]

    def test_namespaces_scoped_to_user(self, db_conn):
        db.kv_set(db_conn, "alice", "ns1", "a", '"1"')
        db.kv_set(db_conn, "bob", "ns2", "b", '"2"')
        ns = db.kv_namespaces(db_conn, "alice")
        assert ns == ["ns1"]


# ============================================================================
# CLI Commands
# ============================================================================


class _FakeArgs:
    """Minimal args object for CLI command tests."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _mock_get_kv_conn(db_conn):
    """Return a factory that yields db_conn as a context manager."""
    @contextmanager
    def _inner(args):
        yield db_conn
    return _inner


class TestCmdKvSet:
    def test_set_outputs_ok(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", key="k", value='"hello"', user="alice", config=None)
        cmd_kv_set(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"

    def test_set_invalid_json_errors(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", key="k", value="not json", user="alice", config=None)
        cmd_kv_set(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "invalid JSON" in out["message"]

    def test_set_persists_value(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", key="k", value='{"x": 1}', user="alice", config=None)
        cmd_kv_set(args)
        result = db.kv_get(db_conn, "alice", "ns", "k")
        assert result["value"] == '{"x": 1}'


class TestCmdKvGet:
    def test_get_existing(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        db.kv_set(db_conn, "alice", "ns", "k", '"hello"')
        args = _FakeArgs(namespace="ns", key="k", user="alice", config=None)
        cmd_kv_get(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["value"] == "hello"

    def test_get_missing(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", key="missing", user="alice", config=None)
        cmd_kv_get(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"

    def test_get_returns_parsed_json(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        db.kv_set(db_conn, "alice", "ns", "k", '{"count": 42}')
        args = _FakeArgs(namespace="ns", key="k", user="alice", config=None)
        cmd_kv_get(args)
        out = json.loads(capsys.readouterr().out)
        assert out["value"] == {"count": 42}


class TestCmdKvList:
    def test_list_empty(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", user="alice", config=None)
        cmd_kv_list(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["count"] == 0
        assert out["entries"] == []

    def test_list_with_entries(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        db.kv_set(db_conn, "alice", "ns", "a", '"1"')
        db.kv_set(db_conn, "alice", "ns", "b", '"2"')
        args = _FakeArgs(namespace="ns", user="alice", config=None)
        cmd_kv_list(args)
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 2
        assert out["entries"][0]["key"] == "a"
        assert out["entries"][0]["value"] == "1"


class TestCmdKvDelete:
    def test_delete_existing(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        db.kv_set(db_conn, "alice", "ns", "k", '"v"')
        args = _FakeArgs(namespace="ns", key="k", user="alice", config=None)
        cmd_kv_delete(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["deleted"] is True

    def test_delete_missing(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        args = _FakeArgs(namespace="ns", key="missing", user="alice", config=None)
        cmd_kv_delete(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"


class TestCmdKvNamespaces:
    def test_namespaces(self, db_conn, capsys, monkeypatch):
        monkeypatch.setattr("istota.cli._get_kv_conn", _mock_get_kv_conn(db_conn))
        db.kv_set(db_conn, "alice", "ns1", "a", '"1"')
        db.kv_set(db_conn, "alice", "ns2", "b", '"2"')
        args = _FakeArgs(namespace=None, user="alice", config=None)
        cmd_kv_namespaces(args)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert sorted(out["namespaces"]) == ["ns1", "ns2"]
