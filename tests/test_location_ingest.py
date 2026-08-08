"""Client-supplied ping identity, replay safety, and the ingest reload path.

The iOS shell (``istota-mobile``) keeps points in an on-device queue and
deletes them only once the server acknowledges the batch, so a batch that
is sent but whose response is lost gets sent again. Every point carries a
``client_id`` the device mints once, which is what lets the second delivery
be recognised as the same point rather than stored as a second visit to
the same place.

Stock Overland sends no ``client_id`` at all, so the column is nullable and
the uniqueness constraint is partial — see ``TestClientIdSchema``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota.location import db as location_db


_needs_fastapi = pytest.mark.skipif(
    pytest.importorskip("fastapi", reason="fastapi not installed") is None,
    reason="fastapi not installed",
)


def _init_db(tmp_path: Path) -> Path:
    path = tmp_path / "location.db"
    location_db.init_db(path)
    return path


def _feature(lon: float, lat: float, **props) -> dict:
    base = {
        "timestamp": "2026-07-30T10:00:00Z",
        "horizontal_accuracy": 5,
        "battery_level": 0.9,
        "motion": ["stationary"],
    }
    base.update(props)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": base,
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestClientIdSchema:
    def test_fresh_db_has_client_id_column(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(location_pings)")}
        assert "client_id" in cols

    def test_fresh_db_records_the_current_schema_version(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row[0] == str(location_db.SCHEMA_VERSION)

    def test_v2_db_migrates_in_place(self, tmp_path):
        """A DB predating the column gains it, its index, and the version."""
        path = tmp_path / "location.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE location_pings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                visit_id INTEGER,
                source TEXT NOT NULL DEFAULT 'overland'
            );
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES('version', '2');
            """
        )
        conn.execute(
            "INSERT INTO location_pings (timestamp, lat, lon) VALUES (?, ?, ?)",
            ("2026-01-01T00:00:00Z", 34.0, -118.0),
        )
        conn.commit()
        conn.close()

        location_db.init_db(path)

        with location_db.connect(path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(location_pings)")}
            assert "client_id" in cols
            indexes = {
                r[1] for r in conn.execute("PRAGMA index_list(location_pings)")
            }
            assert "idx_location_pings_client_id" in indexes
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()[0]
            assert version == str(location_db.SCHEMA_VERSION)
            # The pre-existing row survives, with a NULL client_id.
            row = conn.execute(
                "SELECT client_id FROM location_pings"
            ).fetchone()
            assert row[0] is None

    def test_migration_is_idempotent(self, tmp_path):
        path = _init_db(tmp_path)
        location_db.init_db(path)  # again
        with location_db.connect(path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(location_pings)")]
        assert cols.count("client_id") == 1


# ---------------------------------------------------------------------------
# insert_ping
# ---------------------------------------------------------------------------


class TestInsertPingDedup:
    def test_repeat_client_id_is_ignored(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            first = location_db.insert_ping(
                conn, "2026-07-30T10:00:00Z", 34.0, -118.0, client_id="abc",
            )
            second = location_db.insert_ping(
                conn, "2026-07-30T10:00:00Z", 34.0, -118.0, client_id="abc",
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM location_pings").fetchone()[0]

        assert first > 0
        # 0 is the "nothing was written" signal callers branch on — a
        # duplicate must not look like a fresh ping id.
        assert second == 0
        assert count == 1

    def test_distinct_client_ids_both_land(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(conn, "2026-07-30T10:00:00Z", 34.0, -118.0,
                                    client_id="a")
            location_db.insert_ping(conn, "2026-07-30T10:00:00Z", 34.0, -118.0,
                                    client_id="b")
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM location_pings").fetchone()[0]
        assert count == 2

    def test_null_client_ids_never_collide(self, tmp_path):
        """The Garmin importer and stock Overland send none — every point
        must still land, including byte-identical ones."""
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            for _ in range(3):
                rowid = location_db.insert_ping(
                    conn, "2026-07-30T10:00:00Z", 34.0, -118.0, source="garmin",
                )
                assert rowid > 0
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM location_pings").fetchone()[0]
        assert count == 3

    def test_client_id_survives_the_received_at_override(self, tmp_path):
        """insert_ping has two INSERT paths; both must carry the id."""
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(
                conn, "2026-07-30T10:00:00Z", 34.0, -118.0,
                client_id="dated", received_at="2026-07-01T00:00:00Z",
            )
            conn.commit()
            row = conn.execute(
                "SELECT client_id, received_at FROM location_pings"
            ).fetchone()
        assert row[0] == "dated"
        assert row[1] == "2026-07-01T00:00:00Z"

    def test_ping_model_exposes_client_id(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(conn, "2026-07-30T10:00:00Z", 34.0, -118.0,
                                    client_id="xyz")
            conn.commit()
            latest = location_db.get_latest_ping(conn)
            listed = location_db.get_pings(conn)
        assert latest.client_id == "xyz"
        assert listed[0].client_id == "xyz"


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


@_needs_fastapi
class TestReceiverClientId:
    def test_client_id_is_stored_from_properties(self, tmp_path):
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(conn, _feature(-118.0, 34.0, client_id="dev-1"), [])
            conn.commit()
            row = conn.execute("SELECT client_id FROM location_pings").fetchone()
        assert row[0] == "dev-1"

    def test_stock_overland_payload_still_ingests(self, tmp_path):
        """The regression that matters: no client_id key at all."""
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.03, 37.33]},
            "properties": {
                "timestamp": "2026-02-20T10:30:00-0700",
                "altitude": 80,
                "speed": 0,
                "horizontal_accuracy": 5,
                "motion": ["stationary"],
                "battery_level": 0.92,
                "wifi": "home-wifi",
            },
        }
        with location_db.connect(path) as conn:
            _process_feature(conn, feature, [])
            _process_feature(conn, feature, [])
            conn.commit()
            rows = conn.execute(
                "SELECT client_id FROM location_pings"
            ).fetchall()

        # Two identical stock features are two points, not one — without a
        # client_id there is nothing to dedup on, and inventing one from the
        # payload would silently drop genuine repeat fixes.
        assert len(rows) == 2
        assert all(r[0] is None for r in rows)

    def test_empty_client_id_is_treated_as_absent(self, tmp_path):
        """A client that sends "" must not collapse its whole history.

        An empty string is a perfectly good unique key, so storing it
        verbatim would make every point from that device collide with the
        first one and be silently dropped — a tracker that looks healthy
        and records one row.
        """
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(conn, _feature(-118.0, 34.0, client_id=""), [])
            _process_feature(
                conn,
                _feature(-118.1, 34.1, client_id="",
                         timestamp="2026-07-30T10:05:00Z"),
                [],
            )
            conn.commit()
            rows = conn.execute("SELECT client_id FROM location_pings").fetchall()

        assert len(rows) == 2
        assert all(r[0] is None for r in rows)

    def test_replayed_batch_stores_one_ping(self, tmp_path):
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        feature = _feature(-118.0, 34.0, client_id="dev-1")
        with location_db.connect(path) as conn:
            _process_feature(conn, feature, [])
            _process_feature(conn, feature, [])
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM location_pings").fetchone()[0]
        assert count == 1

    def test_replay_does_not_redrive_the_state_machine(self, tmp_path):
        """A resent point must not count toward the visit it is inside.

        Every ping at the current place increments the open visit's
        ``ping_count``, so a device that resent one batch would inflate the
        visit — and ``ping_count`` is what the reconciler and the walk-by
        filter read. The replay must contribute nothing while a genuinely
        new point still contributes.
        """
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.add_place(conn, "Home", 34.0, -118.0, radius_meters=100)
            places = location_db.get_places(conn)

            first = _feature(-118.0, 34.0, client_id="p1")
            _process_feature(conn, first, places)
            conn.commit()
            state = location_db.get_location_state(conn)
            visit_id = state.current_visit_id
            assert visit_id is not None
            opened_count = conn.execute(
                "SELECT ping_count FROM visits WHERE id = ?", (visit_id,)
            ).fetchone()[0]

            _process_feature(conn, first, places)  # the replay
            conn.commit()
            after_replay = conn.execute(
                "SELECT ping_count FROM visits WHERE id = ?", (visit_id,)
            ).fetchone()[0]

            _process_feature(
                conn,
                _feature(-118.0, 34.0, client_id="p2",
                         timestamp="2026-07-30T10:01:00Z"),
                places,
            )
            conn.commit()
            after_real = conn.execute(
                "SELECT ping_count FROM visits WHERE id = ?", (visit_id,)
            ).fetchone()[0]
            stored = conn.execute(
                "SELECT COUNT(*) FROM location_pings"
            ).fetchone()[0]

        assert after_replay == opened_count, "the replay was counted as a visit ping"
        assert after_real == opened_count + 1, "a genuine point stopped counting"
        assert stored == 2
