"""Tests for the per-user location module (Stage 1).

Covers ``init_db`` semantics (WAL, schema_only_at sentinel, idempotence),
loader gates (``resolve_for_user`` / ``list_users``), the singleton
``set_location_state`` UPSERT, and the framework→per-user migrator
(idempotence, isolation, FK orphans, conflict detection).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from istota.config import Config, UserConfig
from istota.location import (
    LocationContext,
    UserNotFoundError,
    db as location_db,
    list_users,
    resolve_for_user,
)
from istota.location._migrate import MigrationConflict, migrate_legacy_data
from istota.location.workspace import synthesize_location_context


# -- shared fixtures --------------------------------------------------------


@pytest.fixture
def istota_config(tmp_path):
    """Minimal Config that resolves to a workspace under tmp_path."""
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path,
        users={"alice": UserConfig(), "bob": UserConfig()},
    )


@pytest.fixture
def location_path(tmp_path):
    return tmp_path / "location.db"


# -- workspace synthesis ----------------------------------------------------


class TestSynthesize:
    def test_synthesize_default_db_path(self, tmp_path):
        ctx = synthesize_location_context("alice", tmp_path)
        assert ctx.user_id == "alice"
        assert ctx.workspace == tmp_path.resolve()
        assert ctx.db_path == (
            tmp_path / "location" / "data" / "location.db"
        ).resolve()

    def test_synthesize_explicit_db_path(self, tmp_path):
        explicit = tmp_path / "custom.db"
        ctx = synthesize_location_context("alice", tmp_path, db_path=explicit)
        assert ctx.db_path == explicit.resolve()


# -- init_db / connect ------------------------------------------------------


class TestInitDb:
    def test_init_db_creates_schema_idempotent(self, location_path):
        location_db.init_db(location_path)
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        # 5 user tables + schema_meta
        assert {
            "places", "visits", "location_pings", "dismissed_clusters",
            "location_state", "schema_meta",
        } <= tables

    def test_init_db_writes_schema_version(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
        assert row["value"] == "1"

    def test_init_db_writes_schema_only_sentinel_on_fresh(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            row1 = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_only_at'"
            ).fetchone()
        assert row1 is not None
        sentinel_first = row1["value"]
        # Re-init must not overwrite.
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            row2 = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_only_at'"
            ).fetchone()
        assert row2["value"] == sentinel_first

    def test_init_db_sets_wal_mode(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_connect_does_not_re_issue_wal_pragma(self, location_path):
        """``connect`` must not touch journal_mode — re-issuing it
        races with sibling readers and raises 'database is locked'.

        Read the AST to confirm no ``execute`` call mentions
        journal_mode in the body of :func:`connect`.
        """
        import ast
        import inspect
        src = inspect.getsource(location_db.connect)
        tree = ast.parse(src)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        # Drop the docstring before walking.
        body = func.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "journal_mode" not in node.value.lower()

    def test_init_db_concurrent_safe(self, location_path):
        errors: list[BaseException] = []

        def runner():
            try:
                location_db.init_db(location_path)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=runner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        with location_db.connect(location_path) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "location_pings" in tables


# Hook close() onto the sqlite3.Connection returned by connect() for the
# `with` patterns used above. Tests that need an explicit close use the
# ctx-manager protocol; sqlite3.Connection already implements __enter__
# and __exit__ for transaction handling but the conn is left open. We
# rely on tmp_path teardown to close the file handles.


# -- loader -----------------------------------------------------------------


class TestLoader:
    def test_resolve_for_user_returns_context(self, istota_config, tmp_path):
        ctx = resolve_for_user("alice", istota_config)
        assert isinstance(ctx, LocationContext)
        assert ctx.user_id == "alice"
        assert ctx.workspace.is_relative_to(tmp_path.resolve())
        assert ctx.db_path.name == "location.db"

    def test_resolve_for_user_gates_module_disabled(self, tmp_path):
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path,
            users={"alice": UserConfig(disabled_modules=["location"])},
        )
        with pytest.raises(UserNotFoundError):
            resolve_for_user("alice", config)

    def test_resolve_for_user_gates_no_mount(self, tmp_path):
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=None,
            users={"alice": UserConfig()},
        )
        with pytest.raises(UserNotFoundError):
            resolve_for_user("alice", config)

    def test_resolve_for_user_gates_unknown_user(self, istota_config):
        with pytest.raises(UserNotFoundError):
            resolve_for_user("nobody", istota_config)

    def test_resolve_for_user_gates_none_config(self):
        with pytest.raises(UserNotFoundError):
            resolve_for_user("alice", None)

    def test_list_users_filters_disabled(self, tmp_path):
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path,
            users={
                "alice": UserConfig(),
                "bob": UserConfig(disabled_modules=["location"]),
                "carol": UserConfig(),
            },
        )
        assert set(list_users(config)) == {"alice", "carol"}

    def test_list_users_none_config(self):
        assert list_users(None) == []


# -- set_location_state ------------------------------------------------------


class TestSetLocationState:
    def test_first_call_inserts_singleton(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            location_db.set_location_state(
                conn,
                current_place_id=None,
                current_visit_id=None,
                consecutive_count=0,
            )
            conn.commit()
            rows = conn.execute(
                "SELECT id, consecutive_count FROM location_state"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        assert rows[0]["consecutive_count"] == 0

    def test_second_call_updates_singleton(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            place_id = location_db.add_place(conn, "home", 52.0, 13.0)
            visit_id = location_db.open_visit(
                conn, place_id, "home", "2026-05-01T10:00:00Z",
            )
            location_db.set_location_state(
                conn,
                current_place_id=None,
                current_visit_id=None,
                consecutive_count=0,
            )
            location_db.set_location_state(
                conn,
                current_place_id=place_id,
                current_visit_id=visit_id,
                consecutive_count=3,
                last_ping_place_id=place_id,
                exit_started_at=None,
            )
            conn.commit()
            rows = conn.execute(
                "SELECT id, current_place_id, consecutive_count "
                "FROM location_state"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["current_place_id"] == place_id
        assert rows[0]["consecutive_count"] == 3

    def test_check_constraint_blocks_non_singleton(self, location_path):
        location_db.init_db(location_path)
        with location_db.connect(location_path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO location_state (id, consecutive_count) "
                    "VALUES (2, 0)"
                )


# -- migrator ---------------------------------------------------------------


def _make_framework_db(path: Path) -> sqlite3.Connection:
    """Spin up a framework-shaped istota.db with the legacy location tables."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_meters INTEGER NOT NULL DEFAULT 100,
            category TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            notes TEXT,
            UNIQUE(user_id, name)
        );
        CREATE TABLE visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            place_id INTEGER,
            place_name TEXT NOT NULL,
            entered_at TEXT NOT NULL,
            exited_at TEXT,
            duration_sec INTEGER,
            ping_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE location_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            altitude REAL,
            accuracy REAL,
            speed REAL,
            course REAL,
            battery REAL,
            activity_type TEXT,
            wifi TEXT,
            place_id INTEGER,
            visit_id INTEGER
        );
        CREATE TABLE dismissed_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_meters INTEGER NOT NULL,
            dismissed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE location_state (
            user_id TEXT PRIMARY KEY,
            current_place_id INTEGER,
            current_visit_id INTEGER,
            consecutive_count INTEGER DEFAULT 0,
            last_ping_place_id INTEGER,
            exit_started_at TEXT
        );
        """
    )
    conn.commit()
    return conn


def _seed_user_data(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    place_id: int = 1,
    visit_id: int = 1,
    extra_pings: int = 0,
) -> None:
    """Drop a small consistent dataset for one user into framework tables."""
    conn.execute(
        "INSERT INTO places (id, user_id, name, lat, lon, radius_meters) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (place_id, user_id, f"home-{user_id}", 52.0, 13.0, 50),
    )
    conn.execute(
        "INSERT INTO visits (id, user_id, place_id, place_name, entered_at, "
        "exited_at, duration_sec, ping_count) "
        "VALUES (?, ?, ?, ?, '2026-05-01T10:00:00Z', "
        "'2026-05-01T11:00:00Z', 3600, 5)",
        (visit_id, user_id, place_id, f"home-{user_id}"),
    )
    conn.execute(
        "INSERT INTO location_pings (user_id, timestamp, lat, lon, "
        "place_id, visit_id) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, "2026-05-01T10:30:00Z", 52.0, 13.0, place_id, visit_id),
    )
    for i in range(extra_pings):
        conn.execute(
            "INSERT INTO location_pings (user_id, timestamp, lat, lon) "
            "VALUES (?, ?, ?, ?)",
            (user_id, f"2026-05-01T12:{i:02d}:00Z", 52.0, 13.0),
        )
    conn.execute(
        "INSERT INTO dismissed_clusters (user_id, lat, lon, radius_meters) "
        "VALUES (?, ?, ?, ?)",
        (user_id, 52.5, 13.5, 200),
    )
    conn.execute(
        "INSERT INTO location_state (user_id, current_place_id, "
        "current_visit_id, consecutive_count, last_ping_place_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, place_id, visit_id, 4, place_id),
    )
    conn.commit()


def _ctx(user_id: str, tmp_path: Path) -> LocationContext:
    return synthesize_location_context(
        user_id, tmp_path / "ws" / user_id,
        db_path=tmp_path / f"{user_id}_location.db",
    )


class TestMigrate:
    def test_idempotent(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)

        first = migrate_legacy_data(framework_db, ctx)
        second = migrate_legacy_data(framework_db, ctx)

        assert first["location_pings"] == 1
        assert first["places"] == 1
        assert first["visits"] == 1
        assert first["dismissed_clusters"] == 1
        assert first["location_state"] == 1
        assert all(
            second[k] == 0 for k in
            ("location_pings", "places", "visits",
             "dismissed_clusters", "location_state")
        )

    def test_isolates_users(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice", place_id=1, visit_id=1)
        _seed_user_data(fw, "bob", place_id=2, visit_id=2)
        fw.close()
        ctx_a = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx_a)
        with location_db.connect(ctx_a.db_path) as conn:
            names = {
                r[0] for r in conn.execute("SELECT name FROM places")
            }
            ping_count = conn.execute(
                "SELECT COUNT(*) FROM location_pings"
            ).fetchone()[0]
        assert names == {"home-alice"}
        assert ping_count == 1

    def test_preserves_place_ids(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice", place_id=42, visit_id=7)
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            row = conn.execute("SELECT id, name FROM places").fetchone()
        assert row["id"] == 42
        assert row["name"] == "home-alice"

    def test_preserves_fk_integrity(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []

    def test_nulls_orphan_visit_id(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice", place_id=1, visit_id=1)
        # Add a ping pointing at a non-existent visit_id
        fw.execute(
            "INSERT INTO location_pings (user_id, timestamp, lat, lon, "
            "place_id, visit_id) VALUES ('alice', '2026-05-01T13:00:00Z', "
            "52.0, 13.0, 1, 999)"
        )
        fw.commit()
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            orphan_row = conn.execute(
                "SELECT visit_id FROM location_pings "
                "WHERE timestamp = '2026-05-01T13:00:00Z'"
            ).fetchone()
        assert orphan_row["visit_id"] is None

    def test_nulls_orphan_place_id(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice", place_id=1, visit_id=1)
        fw.execute(
            "INSERT INTO location_pings (user_id, timestamp, lat, lon, "
            "place_id) VALUES ('alice', '2026-05-01T14:00:00Z', "
            "52.0, 13.0, 999)"
        )
        fw.commit()
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT place_id FROM location_pings "
                "WHERE timestamp = '2026-05-01T14:00:00Z'"
            ).fetchone()
        assert row["place_id"] is None

    def test_last_ping_place_id_fk_orphan_handled(self, tmp_path):
        """``location_state.last_ping_place_id`` referencing a deleted
        place should be NULLed during migration."""
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        # A user whose location_state references a place that does not
        # exist in places at all.
        fw.execute(
            "INSERT INTO location_state (user_id, current_place_id, "
            "current_visit_id, consecutive_count, last_ping_place_id) "
            "VALUES ('alice', NULL, NULL, 0, 9999)"
        )
        fw.commit()
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT last_ping_place_id FROM location_state WHERE id=1"
            ).fetchone()
        assert row["last_ping_place_id"] is None

    def test_skips_when_sentinel_set(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)
        # Pre-init the target with a sentinel so migrator returns zeros
        # without copying.
        location_db.init_db(ctx.db_path)
        with location_db.connect(ctx.db_path) as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) "
                "VALUES('location_legacy_db_migrated_at', '2026-05-01T00:00:00Z')"
            )
            conn.commit()
        counts = migrate_legacy_data(framework_db, ctx)
        assert all(
            counts[k] == 0 for k in
            ("location_pings", "places", "visits",
             "dismissed_clusters", "location_state")
        )

    def test_aborts_on_unsentinel_data(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)
        # Pre-create target with a place row but no sentinel — refuses.
        location_db.init_db(ctx.db_path)
        with location_db.connect(ctx.db_path) as conn:
            conn.execute(
                "INSERT INTO places (name, lat, lon) VALUES ('x', 0, 0)"
            )
            conn.commit()
        with pytest.raises(MigrationConflict):
            migrate_legacy_data(framework_db, ctx)

    def test_allows_schema_only_target(self, tmp_path):
        """Pre-init'd empty target (with schema_only_at sentinel) is fine."""
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)
        location_db.init_db(ctx.db_path)  # writes schema_only_at
        counts = migrate_legacy_data(framework_db, ctx)
        assert counts["location_pings"] == 1

    def test_writes_sentinel(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice")
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta "
                "WHERE key='location_legacy_db_migrated_at'"
            ).fetchone()
        assert row is not None
        assert "T" in row["value"] or " " in row["value"]  # ISO-like

    def test_handles_missing_framework_tables(self, tmp_path):
        """Stage 4 ran first → framework DB has no legacy tables.
        Migrator treats this as success and writes the sentinel."""
        framework_db = tmp_path / "istota.db"
        fw = sqlite3.connect(framework_db)
        # No legacy tables — just an unrelated framework table.
        fw.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        fw.commit()
        fw.close()
        ctx = _ctx("alice", tmp_path)
        counts = migrate_legacy_data(framework_db, ctx)
        assert counts["location_pings"] == 0
        with location_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta "
                "WHERE key='location_legacy_db_migrated_at'"
            ).fetchone()
        assert row is not None

    def test_post_migration_insert_does_not_collide(self, tmp_path):
        framework_db = tmp_path / "istota.db"
        fw = _make_framework_db(framework_db)
        _seed_user_data(fw, "alice", place_id=42, visit_id=1)
        fw.close()
        ctx = _ctx("alice", tmp_path)
        migrate_legacy_data(framework_db, ctx)
        with location_db.connect(ctx.db_path) as conn:
            new_id = location_db.add_place(conn, "fresh", 1.0, 2.0)
            conn.commit()
        assert new_id > 42
