"""Declared wifi-zone points must not carry a fabricated altitude (ISSUE-229).

The iOS shell substitutes a *declared* coordinate while the device is on the
configured home SSID, rather than reporting a measured fix. That coordinate is
built with ``altitude: -1`` — the shell's "unknown" for a payload slot it has
to fill — and marked on the wire with ``wifi_zone: true``. Nothing on the
server read the marker, so the sentinel was stored as an ordinary altitude and
every reader of ``location_pings.altitude`` got a number the device never
measured.

The scrub matches on the marker, never on the value: -1 m is a legitimate
altitude (Death Valley, the Salton Sea, most of the Netherlands).
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
        "timestamp": "2026-08-07T10:00:00Z",
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


def _zone_feature(lon: float, lat: float, **props) -> dict:
    """What ``wifiZoneLocation`` + ``featureFromLocation:wifiZone:`` emit."""
    return _feature(
        lon,
        lat,
        altitude=-1,
        speed=0,
        course=0,
        horizontal_accuracy=1,
        vertical_accuracy=0,
        wifi_zone=True,
        wifi="home-ssid",
        **props,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestDeclaredPointSchema:
    def test_fresh_db_has_the_new_columns(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(location_pings)")}
        assert "wifi_zone" in cols
        assert "vertical_accuracy" in cols

    def test_fresh_db_records_schema_version_4(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row[0] == "4"

    def test_migration_is_idempotent(self, tmp_path):
        path = _init_db(tmp_path)
        location_db.init_db(path)  # again
        with location_db.connect(path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(location_pings)")]
        assert cols.count("wifi_zone") == 1
        assert cols.count("vertical_accuracy") == 1


def _v3_db(tmp_path: Path) -> Path:
    """A DB at the schema this issue found, with no marker column."""
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
            source TEXT NOT NULL DEFAULT 'overland',
            client_id TEXT
        );
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES('version', '3');
        """
    )
    conn.commit()
    conn.close()
    return path


def _insert_raw(path: Path, **cols) -> None:
    conn = sqlite3.connect(path)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO location_pings ({names}) VALUES ({marks})",
        tuple(cols.values()),
    )
    conn.commit()
    conn.close()


class TestBackfill:
    """Rows already stored carry the -1 with nothing marking them.

    The only handle left is the declared fix's own signature, which is
    narrow: horizontal accuracy of exactly 1 m is not something consumer
    GPS produces, and it arrives with a zeroed speed and course.
    """

    def test_declared_rows_lose_the_sentinel_and_gain_the_marker(self, tmp_path):
        path = _v3_db(tmp_path)
        _insert_raw(
            path,
            timestamp="2026-08-01T10:00:00Z",
            lat=34.0,
            lon=-118.0,
            altitude=-1,
            accuracy=1,
            speed=0,
            course=0,
            wifi="home-ssid",
            source="overland",
        )

        location_db.init_db(path)

        with location_db.connect(path) as conn:
            row = conn.execute(
                "SELECT altitude, wifi_zone FROM location_pings"
            ).fetchone()
        assert row["altitude"] is None
        assert row["wifi_zone"] == 1

    @pytest.mark.parametrize(
        "label,differs",
        [
            # One survivor per conjunct, each differing from the declared
            # signature in exactly one field. A row differing in several at
            # once would survive even if all but one conjunct were dropped,
            # which is what makes the "only the whole signature" claim testable
            # rather than merely asserted.
            ("below sea level but moving", {"course": 180.0}),
            ("below sea level but drifting", {"speed": 2.1}),
            ("below sea level on an ordinary fix", {"accuracy": 8.0}),
            ("parked at a metre of accuracy", {"altitude": 143.0}),
        ],
    )
    def test_a_measured_fix_survives_every_single_difference(
        self, tmp_path, label, differs,
    ):
        """-1 m is a legitimate altitude; only the whole signature matches."""
        path = _v3_db(tmp_path)
        row = {
            "timestamp": "2026-08-01T10:00:00Z",
            "lat": 33.3,
            "lon": -115.8,
            "altitude": -1.0,
            "accuracy": 1.0,
            "speed": 0.0,
            "course": 0.0,
            "source": "overland",
        }
        row.update(differs)
        _insert_raw(path, **row)

        location_db.init_db(path)

        with location_db.connect(path) as conn:
            stored = conn.execute(
                "SELECT altitude, wifi_zone FROM location_pings"
            ).fetchone()
        assert stored["altitude"] == row["altitude"], label
        assert stored["wifi_zone"] == 0, label

    def test_an_imported_track_is_never_touched(self, tmp_path):
        """A Garmin import has no wifi-zone feature to have fired."""
        path = _v3_db(tmp_path)
        _insert_raw(
            path,
            timestamp="2026-08-01T10:00:00Z",
            lat=34.0,
            lon=-118.0,
            altitude=-1,
            accuracy=1,
            speed=0,
            course=0,
            source="garmin",
        )

        location_db.init_db(path)

        with location_db.connect(path) as conn:
            row = conn.execute("SELECT altitude FROM location_pings").fetchone()
        assert row["altitude"] == -1

    def test_the_column_and_the_sweep_land_together(self, tmp_path):
        """The column's own presence is what suppresses the sweep on later
        opens, so a half-applied migration would lose it permanently.

        A duplicate ``client_id`` makes the unique index fail — it is created
        after the column additions, inside the same ``init_db``. While the
        ALTER autocommitted on its own and the UPDATE waited for ``init_db``'s
        commit at the very end, that failure left the column added and the
        sweep rolled back, and the guard then never fired again.
        """
        path = _v3_db(tmp_path)
        for ts in ("2026-08-01T10:00:00Z", "2026-08-01T10:00:01Z"):
            _insert_raw(
                path,
                timestamp=ts,
                lat=34.0,
                lon=-118.0,
                altitude=-1,
                accuracy=1,
                speed=0,
                course=0,
                source="overland",
                client_id="collides",
            )

        with pytest.raises(sqlite3.IntegrityError):
            location_db.init_db(path)

        conn = sqlite3.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(location_pings)")}
        swept = conn.execute(
            "SELECT COUNT(*) FROM location_pings WHERE wifi_zone = 1"
        ).fetchone()[0] if "wifi_zone" in cols else 0
        conn.close()

        # Either both landed or neither did — never the column alone, which is
        # the state the guard reads as "already swept".
        assert ("wifi_zone" in cols) == (swept == 2)

    def test_backfill_runs_once(self, tmp_path):
        """Re-opening must not re-sweep — the guard is the column's absence."""
        path = _v3_db(tmp_path)
        _insert_raw(
            path,
            timestamp="2026-08-01T10:00:00Z",
            lat=34.0,
            lon=-118.0,
            altitude=-1,
            accuracy=1,
            speed=0,
            course=0,
            source="overland",
        )

        location_db.init_db(path)
        # A later measured point that happens to sit at -1 m must survive the
        # second open, which the value-matching sweep would otherwise catch if
        # it ran again with different data in the table.
        with location_db.connect(path) as conn:
            location_db.insert_ping(
                conn, "2026-08-02T10:00:00Z", 33.3, -115.8,
                altitude=-1, accuracy=1, speed=0, course=0,
            )
            conn.commit()

        location_db.init_db(path)

        with location_db.connect(path) as conn:
            row = conn.execute(
                "SELECT altitude FROM location_pings WHERE timestamp = ?",
                ("2026-08-02T10:00:00Z",),
            ).fetchone()
        assert row["altitude"] == -1


# ---------------------------------------------------------------------------
# insert_ping / model
# ---------------------------------------------------------------------------


class TestInsertPing:
    def test_both_fields_round_trip(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(
                conn, "2026-08-07T10:00:00Z", 34.0, -118.0,
                vertical_accuracy=12.5, wifi_zone=True,
            )
            conn.commit()
            latest = location_db.get_latest_ping(conn)
            listed = location_db.get_pings(conn)

        assert latest.vertical_accuracy == 12.5
        assert latest.wifi_zone is True
        assert listed[0].wifi_zone is True

    def test_defaults_are_a_measured_point_with_no_reported_accuracy(self, tmp_path):
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(conn, "2026-08-07T10:00:00Z", 34.0, -118.0)
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.wifi_zone is False
        assert latest.vertical_accuracy is None

    def test_the_received_at_path_carries_them_too(self, tmp_path):
        """insert_ping has two INSERT statements; both must write the fields."""
        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(
                conn, "2026-08-07T10:00:00Z", 34.0, -118.0,
                vertical_accuracy=3.0, wifi_zone=True,
                received_at="2026-08-01T00:00:00Z",
            )
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.wifi_zone is True
        assert latest.vertical_accuracy == 3.0


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


@_needs_fastapi
class TestReceiverScrub:
    def test_a_declared_point_stores_no_altitude(self, tmp_path):
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(conn, _zone_feature(-118.0, 34.0), [])
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.altitude is None
        assert latest.wifi_zone is True
        # The shell sends 0 here, which beside a NULL altitude would read as a
        # perfect vertical fix rather than as no fix at all.
        assert latest.vertical_accuracy is None

    def test_a_declared_point_keeps_everything_else(self, tmp_path):
        """The coordinate and its 1 m accuracy are load-bearing for place
        matching — only the altitude is fabricated."""
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(conn, _zone_feature(-118.0, 34.0), [])
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.lat == 34.0
        assert latest.lon == -118.0
        assert latest.accuracy == 1
        assert latest.wifi == "home-ssid"

    def test_a_measured_point_keeps_its_altitude(self, tmp_path):
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(
                conn,
                _feature(-118.0, 34.0, altitude=143, vertical_accuracy=6),
                [],
            )
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.altitude == 143
        assert latest.vertical_accuracy == 6
        assert latest.wifi_zone is False

    def test_a_measured_point_at_minus_one_metre_keeps_it(self, tmp_path):
        """Matching on the value rather than the marker would eat this."""
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(
                conn,
                _feature(-115.8, 33.3, altitude=-1, vertical_accuracy=9),
                [],
            )
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.altitude == -1

    def test_a_negative_vertical_accuracy_invalidates_the_altitude(self, tmp_path):
        """iOS's documented "this vertical fix is invalid" signal, and the same
        negative-sentinel convention speed and course already get scrubbed by.
        """
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(
                conn,
                _feature(-118.0, 34.0, altitude=0, vertical_accuracy=-1),
                [],
            )
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.altitude is None
        # The signal itself is kept — it is why the altitude is gone.
        assert latest.vertical_accuracy == -1

    def test_stock_overland_sends_neither_key(self, tmp_path):
        """The regression that matters: no wifi_zone, no vertical_accuracy."""
        from istota.webhook_receiver import _process_feature

        path = _init_db(tmp_path)
        with location_db.connect(path) as conn:
            _process_feature(conn, _feature(-122.03, 37.33, altitude=80), [])
            conn.commit()
            latest = location_db.get_latest_ping(conn)

        assert latest.altitude == 80
        assert latest.vertical_accuracy is None
        assert latest.wifi_zone is False
