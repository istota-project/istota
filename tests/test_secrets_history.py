"""Credential versions preserve encrypted values and their binding as one unit."""
import json
import pytest
from istota import db
from istota.credentials import store
from istota.credentials.broker.bindings import get_binding, parse_binding, credential_name

@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-history-key-" * 4)

def put(path, name="example", value="SENTINEL-old-value", source="local"):
    binding = parse_binding("example.com", {}, [], source=source)
    binding["credential"] = "example"
    store.set_secret(path, "alice", "vault_entries", name, value, binding=binding)

def test_update_and_restore(db_path):
    put(db_path)
    store.set_secret(db_path, "alice", "vault_entries", "example", "new", actor="web:alice")
    history = store.list_history(db_path, "alice", "example")
    assert len(history) == 1
    assert history[0]["actor"] == "web:alice"
    assert "SENTINEL" not in json.dumps(history)
    assert store.restore_history(db_path, "alice", history[0]["id"], actor="restore:alice") == ["example"]
    assert store.get_secret(db_path, "alice", "vault_entries", "example") == "SENTINEL-old-value"
    assert len(store.list_history(db_path, "alice", "example")) == 2

def test_same_value_and_other_services_do_not_add_history(db_path):
    put(db_path)
    put(db_path)
    for value in ("old", "new", ""):
        store.set_secret(db_path, "alice", "wallet", "number", value)
    assert store.list_history(db_path, "alice", "example") == []
    with db.get_db(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM secrets_history").fetchone()[0] == 0

def test_delete_batch_restores_bindings_and_members(db_path):
    put(db_path)
    put(db_path, "example_username", "alice")
    assert store.delete_secret(db_path, "alice", "vault_entries", "example", all_fields=True, actor="web:alice")
    history = store.list_history(db_path, "alice", "example")
    assert len(history) == 1
    assert set(history[0]["fields"]) == {"example", "example_username"}
    assert set(store.restore_history(db_path, "alice", history[0]["id"], actor="restore:alice")) == {"example", "example_username"}
    with db.get_db(db_path) as conn:
        assert get_binding(conn, "alice", "example")["hosts"] == ["example.com"]
        assert credential_name(conn, "alice", "example_username") == "example"

def test_restore_refuses_another_source_and_user(db_path):
    put(db_path)
    store.delete_secret(db_path, "alice", "vault_entries", "example")
    history_id = store.list_history(db_path, "alice", "example")[0]["id"]
    put(db_path, value="generated", source="generated")
    with pytest.raises(ValueError, match="history_name_taken"):
        store.restore_history(db_path, "alice", history_id, actor="restore:alice")
    with pytest.raises(ValueError, match="history_not_found"):
        store.restore_history(db_path, "bob", history_id, actor="restore:bob")
    assert store.get_secret(db_path, "alice", "vault_entries", "example") == "generated"

def test_retention_purge_and_undecryptable_history(db_path, monkeypatch):
    for i in range(15):
        put(db_path, value=str(i))
    assert len(store.list_history(db_path, "alice", "example")) == 10
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "rotated-test-history-key-" * 4)
    put(db_path, value="after rotation")
    assert len(store.list_history(db_path, "alice", "example")) == 10
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE secrets_history SET replaced_at = datetime('now', '-181 days')")
        assert store.prune_history(conn, older_than_days=180) == 10
    put(db_path, value="last")
    assert store.purge_history(db_path, "alice", "example") == 1
    assert store.list_history(db_path, "alice", "example") == []


def test_history_and_update_rollback_together(db_path):
    put(db_path)
    with pytest.raises(RuntimeError):
        with db.get_db(db_path) as conn:
            store.set_secret(db_path, "alice", "vault_entries", "example", "changed", connection=conn)
            raise RuntimeError("abort")
    assert store.list_history(db_path, "alice", "example") == []
    assert store.get_secret(db_path, "alice", "vault_entries", "example") == "SENTINEL-old-value"
    store.upsert_secret(db_path, "alice", "vault_entries", "example", "new", actor="import")
    assert store.list_history(db_path, "alice", "example")[0]["actor"] == "import"
    with db.get_db(db_path) as conn:
        assert "SENTINEL" not in "\n".join(conn.iterdump())
