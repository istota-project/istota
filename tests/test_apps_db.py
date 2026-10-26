"""The per-user ``apps.db``: schema v1 and the query functions over it."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from istota.apps import db as apps_db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "alice" / "apps.db"
    apps_db.init_db(path)
    with apps_db.connect(path) as c:
        yield c


def _install(conn, app_id="board", *, document=b"<html>v</html>", origin="draft"):
    apps_db.upsert_app(conn, app_id, name="Board", icon="layout", description="", origin=origin)
    version = apps_db.insert_version(
        conn, app_id,
        document=document,
        manifest={"id": app_id},
        source={"index.html": document.decode()},
        narrowest="phone",
        script_hashes=["sha256-x"],
        csp="default-src 'none'",
        warnings=[],
    )
    apps_db.set_current_version(conn, app_id, version)
    return version


def _pending(conn, app_id="board", document=b"<html>p</html>"):
    return apps_db.replace_pending(
        conn, app_id=app_id, document=document,
        manifest={"id": app_id}, source={}, script_hashes=[], csp="c",
        warnings=["w"], origin="draft",
    )


class TestInitDb:
    def test_a_fresh_file_gets_every_table_and_the_version(self, tmp_path):
        path = tmp_path / "nested" / "apps.db"
        apps_db.init_db(path)
        with apps_db.connect(path) as c:
            tables = {
                r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            version = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        assert {
            "apps", "app_documents", "app_versions", "app_grants", "app_pending",
            "app_starter_state", "app_calls", "app_errors",
        } <= tables
        assert version["value"] == str(apps_db.SCHEMA_VERSION) == "1"
        assert mode == "wal"

    def test_it_is_idempotent(self, tmp_path):
        path = tmp_path / "apps.db"
        apps_db.init_db(path)
        with apps_db.connect(path) as c:
            _install(c)
            c.commit()
        apps_db.init_db(path)
        with apps_db.connect(path) as c:
            assert apps_db.get_app(c, "board")["current_version"] == 1


class TestAppsAndVersions:
    def test_versions_are_numbered_per_app(self, conn):
        assert _install(conn) == 1
        assert _install(conn, document=b"<html>v2</html>") == 2
        assert _install(conn, "ledger") == 1
        assert [v["version"] for v in apps_db.list_versions(conn, "board")] == [1, 2]
        assert apps_db.get_app(conn, "board")["current_version"] == 2
        assert [a["app_id"] for a in apps_db.list_apps(conn)] == ["board", "ledger"]

    def test_a_version_round_trips_its_json_and_its_document(self, conn):
        _install(conn)
        v = apps_db.get_version(conn, "board", 1)
        assert v["manifest"] == {"id": "board"}
        assert v["script_hashes"] == ["sha256-x"]
        assert v["content_hash"] == hashlib.sha256(b"<html>v</html>").hexdigest()
        assert apps_db.get_document(conn, v["content_hash"]) == b"<html>v</html>"

    def test_two_versions_with_one_document_store_it_once(self, conn):
        _install(conn)
        _install(conn)
        assert conn.execute("SELECT COUNT(*) FROM app_documents").fetchone()[0] == 1

    def test_the_origin_is_checked(self, conn):
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            apps_db.upsert_app(conn, "x", name="X", icon="i", description="", origin="agent")

    def test_the_current_version_cannot_be_deleted(self, conn):
        _install(conn)
        with pytest.raises(ValueError):
            apps_db.delete_version(conn, "board", 1)

    def test_deleting_a_version_takes_its_grants(self, conn):
        _install(conn)
        _install(conn, document=b"<html>v2</html>")
        apps_db.set_grants(conn, "board", 1, {"kv:own": None})
        apps_db.delete_version(conn, "board", 1)
        assert apps_db.get_version(conn, "board", 1) is None
        assert apps_db.get_grants(conn, "board", 1) == {}


class TestGrants:
    def test_grants_are_per_version_and_replaced_whole(self, conn):
        _install(conn)
        _install(conn, document=b"<html>v2</html>")
        apps_db.set_grants(conn, "board", 1, {"kv:own": None, "files:read": "notes"})
        apps_db.set_grants(conn, "board", 2, {"kv:own": None})
        assert apps_db.get_grants(conn, "board", 1) == {"kv:own": None, "files:read": "notes"}
        apps_db.set_grants(conn, "board", 1, {"files:own": None})
        assert apps_db.get_grants(conn, "board", 1) == {"files:own": None}
        assert apps_db.get_grants(conn, "board", 2) == {"kv:own": None}


class TestPending:
    def test_one_pending_install_per_app(self, conn):
        first = _pending(conn)
        second = _pending(conn, document=b"<html>p2</html>")
        assert first != second
        assert apps_db.get_pending(conn, first) is None
        row = apps_db.get_pending(conn, second)
        assert row["document"] == b"<html>p2</html>"
        assert row["warnings"] == ["w"]
        assert conn.execute("SELECT COUNT(*) FROM app_pending").fetchone()[0] == 1

    def test_the_unique_index_holds_without_the_function(self, conn):
        import sqlite3

        _pending(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO app_pending (pending_id, app_id, content_hash, manifest_json,"
                " source_json, document, script_hashes_json, csp, warnings_json, origin)"
                " VALUES ('other', 'board', 'h', '{}', '{}', x'00', '[]', '', '[]', 'draft')"
            )

    def test_delete_pending(self, conn):
        pid = _pending(conn)
        apps_db.delete_pending(conn, pid)
        assert apps_db.get_pending(conn, pid) is None
        assert apps_db.list_pending(conn) == []


class TestCalls:
    def test_a_call_stores_a_digest_and_not_the_arguments(self, conn):
        apps_db.insert_call(
            conn, app_id="board", version=1, capability="kv:own", op="get",
            args={"key": "secret-ish", "b": 1}, status="ok", retention_days=30,
        )
        row = conn.execute("SELECT * FROM app_calls").fetchone()
        canonical = json.dumps({"b": 1, "key": "secret-ish"}, sort_keys=True, separators=(",", ":"))
        assert row["args_digest"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert "secret-ish" not in json.dumps(dict(row))

    def test_old_calls_are_pruned_on_insert(self, conn):
        now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        for days_ago in (40, 31, 29, 0):
            apps_db.insert_call(
                conn, app_id="board", version=1, capability="kv:own", op="get", args={},
                status="ok", retention_days=1000, now=now - timedelta(days=days_ago),
            )
        apps_db.insert_call(
            conn, app_id="board", version=1, capability="kv:own", op="get", args={},
            status="ok", retention_days=30, now=now,
        )
        ats = [r["at"] for r in apps_db.list_calls(conn, "board")]
        assert ats == ["2026-10-25 12:00:00", "2026-10-25 12:00:00", "2026-09-26 12:00:00"]

    def test_recent_calls_are_counted_per_app(self, conn):
        now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        for seconds_ago in (90, 30, 5):
            apps_db.insert_call(
                conn, app_id="board", version=1, capability="kv:own", op="get", args={},
                status="ok", retention_days=30, now=now - timedelta(seconds=seconds_ago),
            )
        apps_db.insert_call(
            conn, app_id="ledger", version=1, capability="kv:own", op="get", args={},
            status="ok", retention_days=30, now=now,
        )
        since = now - timedelta(seconds=60)
        assert apps_db.count_recent_calls(conn, "board", since) == 2
        assert apps_db.count_recent_calls(conn, "ledger", since) == 1


class TestErrors:
    def test_errors_are_trimmed_per_version(self, conn):
        for i in range(5):
            apps_db.insert_error(
                conn, app_id="board", version=1, kind="error", message=f"m{i}", keep=3,
            )
        apps_db.insert_error(
            conn, app_id="board", version=2, kind="navigated", message="nav", keep=3,
        )
        assert [e["message"] for e in apps_db.list_errors(conn, "board", 1)] == ["m4", "m3", "m2"]
        assert [e["message"] for e in apps_db.list_errors(conn, "board", 2)] == ["nav"]

    def test_the_kind_is_checked(self, conn):
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            apps_db.insert_error(conn, app_id="board", version=1, kind="bogus", message="m", keep=3)


class TestStarterState:
    def test_round_trip(self, conn):
        assert apps_db.get_starter_state(conn, "board") is None
        apps_db.set_starter_state(conn, "board", installed_hash="h1", dismissed_at=None)
        apps_db.set_starter_state(conn, "board", installed_hash="h2", dismissed_at="2026-10-25")
        state = apps_db.get_starter_state(conn, "board")
        assert state["installed_hash"] == "h2"
        assert state["dismissed_at"] == "2026-10-25"


class TestDocumentGc:
    def test_only_unreferenced_documents_go(self, conn):
        _install(conn, document=b"<html>a</html>")
        _install(conn, document=b"<html>b</html>")
        pending_doc = b"<html>p</html>"
        _pending(conn, document=pending_doc)
        conn.execute(
            "INSERT INTO app_documents (content_hash, document, size) VALUES (?, ?, ?)",
            (hashlib.sha256(pending_doc).hexdigest(), pending_doc, len(pending_doc)),
        )
        conn.execute(
            "INSERT INTO app_documents (content_hash, document, size) VALUES ('orphan', x'00', 1)"
        )
        apps_db.delete_version(conn, "board", 1)
        assert apps_db.gc_documents(conn) == 2
        left = {r[0] for r in conn.execute("SELECT content_hash FROM app_documents")}
        assert left == {
            hashlib.sha256(b"<html>b</html>").hexdigest(),
            hashlib.sha256(pending_doc).hexdigest(),
        }


class TestDeleteApp:
    def test_it_removes_the_app_rows_and_keeps_starter_state(self, conn):
        _install(conn)
        apps_db.set_grants(conn, "board", 1, {"kv:own": None})
        _pending(conn)
        apps_db.insert_call(
            conn, app_id="board", version=1, capability="kv:own", op="get", args={},
            status="ok", retention_days=30,
        )
        apps_db.insert_error(conn, app_id="board", version=1, kind="error", message="m", keep=3)
        apps_db.set_starter_state(conn, "board", installed_hash="h", dismissed_at="now")
        _install(conn, "ledger")

        apps_db.delete_app(conn, "board")

        assert apps_db.get_app(conn, "board") is None
        for table in ("app_versions", "app_grants", "app_pending", "app_calls", "app_errors"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE app_id='board'").fetchone()[0]
            assert n == 0, table
        assert apps_db.get_starter_state(conn, "board")["dismissed_at"] == "now"
        assert apps_db.get_app(conn, "ledger") is not None
