"""Tests for the Garmin track importer core (istota.location.garmin_import).

The pure logic (parse_ts / filter_shadowed / downsample / parse_polyline)
is tested without Garmin or the live DB; the DB-glue tests run against a
temp per-user location.db built from istota.location.db.init_db.
"""

from __future__ import annotations

import pytest

from istota.location import db as location_db
from istota.location import garmin_import as igt


TP = igt.TrackPoint


def _tp(ts, lat, lon, at="running"):
    return TP(timestamp=ts, lat=lat, lon=lon, altitude=None, speed=None,
              activity_type=at)


# ---------------------------------------------------------------------------
# parse_ts / epoch conversion
# ---------------------------------------------------------------------------


class TestParseTs:
    def test_z_form(self):
        assert igt.parse_ts("2026-07-08T10:00:00Z") == pytest.approx(
            igt.parse_ts("2026-07-08T10:00:00+00:00")
        )

    def test_offset_form(self):
        # +02:00 is two hours ahead of UTC → smaller epoch than the same
        # wall-clock in UTC.
        a = igt.parse_ts("2026-07-08T12:00:00+02:00")
        b = igt.parse_ts("2026-07-08T10:00:00Z")
        assert a == pytest.approx(b)

    def test_microseconds_no_z(self):
        # Overland's fallback path: offset-aware, microseconds, no Z.
        e = igt.parse_ts("2026-07-08T10:00:00.500000+00:00")
        assert e == pytest.approx(igt.parse_ts("2026-07-08T10:00:00Z") + 0.5)

    def test_naive_assumed_utc(self):
        assert igt.parse_ts("2026-07-08T10:00:00") == pytest.approx(
            igt.parse_ts("2026-07-08T10:00:00Z")
        )

    def test_epoch_passthrough(self):
        assert igt.parse_ts(1_700_000_000) == 1_700_000_000.0

    def test_bad_raises(self):
        with pytest.raises(ValueError):
            igt.parse_ts("")
        with pytest.raises(ValueError):
            igt.parse_ts(None)

    def test_epoch_ms_to_iso_z(self):
        # 2021-11-14T22:13:20Z == 1_700_000_000_000 ms
        assert igt.epoch_ms_to_iso_z(1_700_000_000_000) == "2023-11-14T22:13:20Z"

    def test_epoch_ms_bad(self):
        with pytest.raises(ValueError):
            igt.epoch_ms_to_iso_z("nope")


# ---------------------------------------------------------------------------
# collapse_subtype / parse_polyline
# ---------------------------------------------------------------------------


class TestParsePolyline:
    def test_subtype_collapses_to_parent(self):
        assert igt.collapse_subtype("trail_running") == "running"
        assert igt.collapse_subtype("casual_walking") == "walking"
        assert igt.collapse_subtype("unknown_x") == "unknown_x"

    def test_parses_points(self):
        details = {"geoPolylineDTO": {"polyline": [
            {"lat": 34.0, "lon": -118.0, "altitude": 100.0, "speed": 2.5,
             "time": 1_700_000_000_000},
            {"lat": 34.1, "lon": -118.1, "altitude": 110.0, "speed": 3.0,
             "time": 1_700_000_010_000},
        ]}}
        pts = igt.parse_polyline(details, "trail_running")
        assert len(pts) == 2
        assert pts[0].activity_type == "running"   # subtype collapsed
        assert pts[0].timestamp == "2023-11-14T22:13:20Z"
        assert pts[0].lat == 34.0 and pts[0].speed == 2.5

    def test_empty_polyline(self):
        assert igt.parse_polyline({"geoPolylineDTO": {"polyline": []}}, "running") == []
        assert igt.parse_polyline({}, "running") == []
        assert igt.parse_polyline(None, "running") == []

    def test_skips_bad_points(self):
        details = {"geoPolylineDTO": {"polyline": [
            {"lat": 34.0, "lon": -118.0, "time": 1_700_000_000_000},
            {"lat": None, "lon": -118.0, "time": 1_700_000_010_000},   # no lat
            {"lat": 34.2, "lon": -118.2, "time": "bad"},               # bad ts
        ]}}
        pts = igt.parse_polyline(details, "running")
        assert len(pts) == 1


# ---------------------------------------------------------------------------
# downsample
# ---------------------------------------------------------------------------


class TestDownsample:
    def _seq(self, n, step=1):
        base = 1_700_000_000
        return [
            _tp(igt.epoch_ms_to_iso_z((base + i * step) * 1000), 34.0 + i * 1e-4,
                -118.0)
            for i in range(n)
        ]

    def test_keeps_first_and_last(self):
        pts = self._seq(100, step=1)   # 1 Hz, 100 pts
        out = igt.downsample(pts, 10)
        assert out[0] is pts[0]
        assert out[-1] is pts[-1]
        # ~ every 10s → ~11 points
        assert 9 <= len(out) <= 12

    def test_noop_small_inputs(self):
        assert igt.downsample([], 10) == []
        one = self._seq(1)
        assert igt.downsample(one, 10) == one
        two = self._seq(2)
        assert igt.downsample(two, 10) == two

    def test_noop_nonpositive_interval(self):
        pts = self._seq(10)
        assert igt.downsample(pts, 0) == pts


# ---------------------------------------------------------------------------
# filter_shadowed — the primary test
# ---------------------------------------------------------------------------


class TestFilterShadowed:
    BAND = 300.0
    RADIUS = 150.0

    def _run(self, lat, lon):
        # A three-point garmin track 30s apart around 10:00:30.
        return [
            _tp("2026-07-08T10:00:00Z", lat, lon),
            _tp("2026-07-08T10:00:30Z", lat + 3e-4, lon),
            _tp("2026-07-08T10:01:00Z", lat + 6e-4, lon),
        ]

    def test_no_native_keeps_all(self):
        pts = self._run(34.0, -118.0)
        assert igt.filter_shadowed(pts, [], self.BAND, self.RADIUS) == pts

    def test_phone_with_you_shadows_all(self):
        """Native pings near in time AND space (phone tracked the run) →
        every point shadowed → whole track skipped."""
        pts = self._run(34.0, -118.0)
        native = [
            (igt.parse_ts("2026-07-08T10:00:05Z"), 34.0, -118.0),
            (igt.parse_ts("2026-07-08T10:00:35Z"), 34.0003, -118.0),
            (igt.parse_ts("2026-07-08T10:01:05Z"), 34.0006, -118.0),
        ]
        assert igt.filter_shadowed(pts, native, self.BAND, self.RADIUS) == []

    def test_phone_at_home_keeps_all(self):
        """THE regression that killed the temporal-only design: the phone is
        at home (native pings near in TIME but kilometres away in SPACE), the
        run is elsewhere. All Garmin points must survive."""
        pts = self._run(34.05, -118.30)   # run route
        home = [
            (igt.parse_ts("2026-07-08T10:00:05Z"), 34.00, -118.00),  # ~30 km away
            (igt.parse_ts("2026-07-08T10:00:35Z"), 34.00, -118.00),
            (igt.parse_ts("2026-07-08T10:01:05Z"), 34.00, -118.00),
        ]
        assert igt.filter_shadowed(pts, home, self.BAND, self.RADIUS) == pts

    def test_phone_dies_midway_keeps_tail(self):
        """Native covers only the first part (phone died mid-run); points
        beyond the band from the last native survive. Uses a tight band so
        the 60s track exposes the gap (band=300 would have one native ping
        own the whole minute — itself correct)."""
        pts = self._run(34.0, -118.0)   # pts at :00, :30, :01:00
        native = [
            (igt.parse_ts("2026-07-08T10:00:02Z"), 34.0, -118.0),  # near pt0
        ]
        out = igt.filter_shadowed(pts, native, band_sec=20.0, radius_m=self.RADIUS)
        assert out == pts[1:]   # pt0 (:00, 2s away) shadowed; :30/:01:00 kept

    def test_radius_boundary(self):
        pts = [_tp("2026-07-08T10:00:00Z", 34.0, -118.0)]
        # A native point ~150 m south. 0.00135 deg lat ≈ 150 m.
        near = [(igt.parse_ts("2026-07-08T10:00:00Z"), 34.0 - 0.00135, -118.0)]
        # Just inside 200 m radius → shadowed; just outside 100 m → kept.
        assert igt.filter_shadowed(pts, near, self.BAND, 200.0) == []
        assert igt.filter_shadowed(pts, near, self.BAND, 100.0) == pts

    def test_band_boundary(self):
        pts = [_tp("2026-07-08T10:05:00Z", 34.0, -118.0)]
        # Native point 200s earlier, same spot.
        near = [(igt.parse_ts("2026-07-08T10:01:40Z"), 34.0, -118.0)]
        assert igt.filter_shadowed(pts, near, 300.0, self.RADIUS) == []   # within band
        assert igt.filter_shadowed(pts, near, 100.0, self.RADIUS) == pts  # outside band


# ---------------------------------------------------------------------------
# DB glue against a temp location.db
# ---------------------------------------------------------------------------


@pytest.fixture
def loc_db(tmp_path):
    path = tmp_path / "location.db"
    location_db.init_db(path)
    return path


class TestDbGlue:
    def test_load_native_excludes_garmin_and_windows(self, loc_db):
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:00Z", 34.0, -118.0,
                                    source="overland")
            location_db.insert_ping(conn, "2026-07-08T10:00:10Z", 34.1, -118.1,
                                    source="garmin")           # excluded
            location_db.insert_ping(conn, "2026-07-08T20:00:00Z", 34.2, -118.2,
                                    source="overland")          # outside window
            conn.commit()
            native = igt.load_native_points(
                conn, "2026-07-08T10:00:00Z", "2026-07-08T10:01:00Z", 300.0,
            )
        assert len(native) == 1
        assert native[0][1] == 34.0

    def test_insert_points_are_placeless_garmin_with_received_at(self, loc_db):
        pts = [_tp("2026-07-08T10:00:00Z", 34.0, -118.0)]
        with location_db.connect(loc_db) as conn:
            igt.insert_points(conn, pts)
            conn.commit()
            row = conn.execute(
                "SELECT source, place_id, received_at FROM location_pings"
            ).fetchone()
        assert row["source"] == "garmin"
        assert row["place_id"] is None
        assert row["received_at"] == "2026-07-08T10:00:00Z"

    def test_evict_removes_only_garmin_in_span(self, loc_db):
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:00Z", 34.0, -118.0,
                                    source="overland")          # keep (native)
            location_db.insert_ping(conn, "2026-07-08T10:00:30Z", 34.0, -118.0,
                                    source="garmin")            # evict (in span)
            location_db.insert_ping(conn, "2026-07-08T23:00:00Z", 34.0, -118.0,
                                    source="garmin")            # keep (out of span)
            conn.commit()
            n = igt.evict_activity_imports(
                conn, "2026-07-08T10:00:00Z", "2026-07-08T10:01:00Z",
            )
            conn.commit()
            sources = [r["source"] for r in conn.execute(
                "SELECT source FROM location_pings ORDER BY timestamp"
            )]
        assert n == 1
        assert sources == ["overland", "garmin"]  # native + out-of-span garmin

    def test_evict_then_reinsert_idempotent(self, loc_db):
        """Re-running converges: a native ping is never touched, and a late
        native ping in a gap evicts the now-covered import."""
        pts = [
            _tp("2026-07-08T10:00:00Z", 34.05, -118.30),
            _tp("2026-07-08T10:00:30Z", 34.051, -118.30),
        ]
        span = ("2026-07-08T10:00:00Z", "2026-07-08T10:00:30Z")

        def import_once():
            with location_db.connect(loc_db) as conn:
                igt.evict_activity_imports(conn, *span)
                native = igt.load_native_points(conn, *span, 300.0)
                kept = igt.filter_shadowed(pts, native, 300.0, 150.0)
                igt.insert_points(conn, kept)
                conn.commit()

        # Seed a native ping far from the run (phone at home).
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:05Z", 34.0, -118.0,
                                    source="overland")
            conn.commit()

        import_once()
        import_once()   # idempotent — no accumulation
        with location_db.connect(loc_db) as conn:
            garmin_count = conn.execute(
                "SELECT COUNT(*) c FROM location_pings WHERE source='garmin'"
            ).fetchone()["c"]
            native_count = conn.execute(
                "SELECT COUNT(*) c FROM location_pings WHERE source='overland'"
            ).fetchone()["c"]
        assert garmin_count == 2      # both track points, once
        assert native_count == 1      # native never deleted

        # Now a LATE native upload lands right on the run route → next run
        # evicts the now-covered imports.
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:02Z", 34.05, -118.30,
                                    source="overland")
            location_db.insert_ping(conn, "2026-07-08T10:00:32Z", 34.051, -118.30,
                                    source="overland")
            conn.commit()
        import_once()
        with location_db.connect(loc_db) as conn:
            garmin_count = conn.execute(
                "SELECT COUNT(*) c FROM location_pings WHERE source='garmin'"
            ).fetchone()["c"]
        assert garmin_count == 0      # native now covers the route → all evicted
