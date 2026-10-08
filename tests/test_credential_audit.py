import pytest
from istota import db

def test_audit_validation_order_and_pruning(db_path):
    from istota.credentials import audit
    with db.get_db(db_path) as conn:
        with pytest.raises(ValueError):
            audit.record(conn, "alice", action="unknown", actor="web:alice")
        with pytest.raises(ValueError):
            audit.record(conn, "alice", action="reveal", actor="web:alice", detail={"nested": {"value": "secret"}})
        audit.record(conn, "alice", action="reveal", actor="web:alice", name="example", detail={"host": "x" * 250})
        audit.record(conn, "alice", action="delete", actor="web:alice", name="example")
        rows = audit.recent(conn, "alice")
        assert [r["action"] for r in rows] == ["delete", "reveal"]
        assert len(rows[1]["detail"]["host"]) == 200
        assert audit.recent(conn, "bob") == []
        conn.execute("UPDATE credential_audit SET at = datetime('now', '-366 days')")
        assert audit.prune(conn, older_than_days=365) == 2
