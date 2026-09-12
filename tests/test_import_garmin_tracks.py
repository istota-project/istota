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

    def test_epoch_ms_to_iso_z_raises_value_error_on_every_bad_value(self):
        """Its one caller catches ValueError alone, so an OverflowError or
        OSError escaping here aborts a whole activity import."""
        for bad in (float("inf"), float("-inf"), float("nan"), 1e20, -1e20,
                    "x", None, True):
            with pytest.raises(ValueError):
                igt.epoch_ms_to_iso_z(bad)

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

    def test_a_non_finite_time_skips_the_point_rather_than_the_activity(self):
        """``fromtimestamp`` answers inf with OverflowError or OSError, which
        the caller's ``except ValueError`` does not catch — so one malformed
        point aborted the whole activity import."""
        details = {"geoPolylineDTO": {"polyline": [
            {"lat": 34.0, "lon": -118.0, "time": float("inf")},
            {"lat": 34.1, "lon": -118.1, "time": 1e20},    # finite, out of range
            {"lat": 34.2, "lon": -118.2, "time": 1_700_000_000_000},
        ]}}
        pts = igt.parse_polyline(details, "running")
        assert len(pts) == 1
        assert pts[0].lat == 34.2

    def test_a_non_finite_altitude_or_speed_is_dropped(self):
        # json.loads accepts the bare Infinity literal, and SQLite stores inf
        # in a REAL column — after which json.dumps emits invalid JSON.
        details = {"geoPolylineDTO": {"polyline": [
            {"lat": 34.0, "lon": -118.0, "altitude": float("inf"),
             "speed": float("nan"), "time": 1_700_000_000_000},
        ]}}
        pt = igt.parse_polyline(details, "running")[0]
        assert pt.altitude is None and pt.speed is None


# ---------------------------------------------------------------------------
# Elevation, which the polyline DTO does not carry (ISSUE-488)
# ---------------------------------------------------------------------------


def _metrics_details(polyline, *, rows, elevation_key="directElevation",
                     unit="meter"):
    """A get_activity_details payload shaped like the real one: a polyline
    with null altitude, plus the chart half carrying per-point elevation.

    ``rows`` is [(epoch_ms, elevation), ...]; the metric row layout puts
    timestamp at index 0 and elevation at index 2, with an unrelated
    column between them so an index mix-up cannot pass.
    """
    return {
        "geoPolylineDTO": {"polyline": polyline},
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0,
             "unit": {"key": "gmt"}},
            {"key": "directHeartRate", "metricsIndex": 1,
             "unit": {"key": "bpm"}},
            {"key": elevation_key, "metricsIndex": 2,
             "unit": {"key": unit} if unit else None},
        ],
        "activityDetailMetrics": [
            {"metrics": [ms, 140.0, elev]} for ms, elev in rows
        ],
    }


class TestElevationFromMetrics:
    """The polyline DTO carries lat/lon/time/speed and a null altitude; the
    per-point elevation is in activityDetailMetrics. ISSUE-488."""

    def test_null_polyline_altitude_is_filled_from_metrics(self):
        base = 1_700_000_000_000
        details = _metrics_details(
            [
                {"lat": 34.0, "lon": -118.0, "altitude": None, "speed": 2.5,
                 "time": base},
                {"lat": 34.1, "lon": -118.1, "altitude": None, "speed": 3.0,
                 "time": base + 10_000},
            ],
            rows=[(base, 300.0), (base + 10_000, 310.0)],
        )
        pts = igt.parse_polyline(details, "hiking")
        assert [p.altitude for p in pts] == [300.0, 310.0]

    def test_polyline_altitude_wins_when_present(self):
        base = 1_700_000_000_000
        details = _metrics_details(
            [{"lat": 34.0, "lon": -118.0, "altitude": 100.0, "speed": 2.5,
              "time": base}],
            rows=[(base, 300.0)],
        )
        assert igt.parse_polyline(details, "hiking")[0].altitude == 100.0

    def test_nearest_sample_within_tolerance(self):
        # The two arrays are sampled independently, so a polyline point
        # rarely lands on a metric timestamp exactly.
        base = 1_700_000_000_000
        details = _metrics_details(
            [{"lat": 34.0, "lon": -118.0, "altitude": None, "time": base + 4_000}],
            rows=[(base, 300.0), (base + 5_000, 350.0)],
        )
        assert igt.parse_polyline(details, "hiking")[0].altitude == 350.0

    def test_no_sample_within_tolerance_stays_null(self):
        base = 1_700_000_000_000
        details = _metrics_details(
            [{"lat": 34.0, "lon": -118.0, "altitude": None, "time": base}],
            rows=[(base + 600_000, 300.0)],
        )
        assert igt.parse_polyline(details, "hiking")[0].altitude is None

    def test_gps_elevation_is_the_fallback_key(self):
        base = 1_700_000_000_000
        details = _metrics_details(
            [{"lat": 34.0, "lon": -118.0, "altitude": None, "time": base}],
            rows=[(base, 300.0)],
            elevation_key="directGpsElevation",
        )
        assert igt.parse_polyline(details, "hiking")[0].altitude == 300.0

    def test_missing_metrics_half_leaves_altitude_null(self):
        base = 1_700_000_000_000
        details = {"geoPolylineDTO": {"polyline": [
            {"lat": 34.0, "lon": -118.0, "altitude": None, "time": base},
        ]}}
        assert igt.parse_polyline(details, "hiking")[0].altitude is None


class TestParseElevationSeries:
    """The shape of the metrics half is read permissively: anything not
    recognised yields no series, and altitude stays NULL."""

    BASE = 1_700_000_000_000

    def _series(self, descriptors, rows):
        return igt.parse_elevation_series({
            "metricDescriptors": descriptors,
            "activityDetailMetrics": rows,
        })

    def test_reads_index_from_the_descriptor_not_position(self):
        # Elevation declared at index 2 while sitting third in the
        # descriptor list, with the descriptors out of index order.
        series = self._series(
            [
                {"key": "directElevation", "metricsIndex": 2},
                {"key": "directTimestamp", "metricsIndex": 0},
            ],
            [{"metrics": [self.BASE, 99.0, 300.0]}],
        )
        assert series == [(self.BASE / 1000.0, 300.0)]

    def test_direct_elevation_wins_over_gps_elevation(self):
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directGpsElevation", "metricsIndex": 1},
                {"key": "directElevation", "metricsIndex": 2},
            ],
            [{"metrics": [self.BASE, 111.0, 300.0]}],
        )
        assert series[0][1] == 300.0

        # ...whichever order the descriptors arrive in.
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 2},
                {"key": "directGpsElevation", "metricsIndex": 1},
            ],
            [{"metrics": [self.BASE, 111.0, 300.0]}],
        )
        assert series[0][1] == 300.0

    def test_feet_are_converted_to_metres(self):
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "foot"}},
            ],
            [{"metrics": [self.BASE, 1000.0]}],
        )
        assert series[0][1] == pytest.approx(304.8)

    @pytest.mark.parametrize("field", ["key", "unitKey", "displayUnit"])
    def test_the_unit_is_read_under_any_known_spelling(self, field):
        """Which field of the unit object names the unit is the unverified
        half of ISSUE-488. Reading only ``key`` made the fail-closed claim
        rest on the fixtures sharing the code's own guess, so a real payload
        spelling it otherwise would have stored feet as metres."""
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {field: "foot", "factor": 1.0}},
            ],
            [{"metrics": [self.BASE, 1000.0]}],
        )
        assert series[0][1] == pytest.approx(304.8)

    def test_unrecognised_unit_drops_the_series(self):
        # Storing a number in an unknown unit is worse than storing nothing:
        # the column is metres everywhere else.
        assert self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "furlong"}},
            ],
            [{"metrics": [self.BASE, 1000.0]}],
        ) == []

    def test_a_unit_object_naming_nothing_we_read_fails_closed(self):
        """A present unit object is Garmin telling us something; failing to
        read it is not the same as there being no unit at all, which is the
        one case that may be assumed to be metres."""
        assert self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"id": 4, "factor": 1.0}},
            ],
            [{"metrics": [self.BASE, 1000.0]}],
        ) == []

    def test_a_declared_factor_other_than_one_drops_the_series(self):
        # Whether it multiplies or divides is exactly what cannot be verified
        # here, and a factor of 100 on a "meter" column is the same silent
        # wrongness an unknown unit is refused for.
        assert self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "meter", "factor": 100.0}},
            ],
            [{"metrics": [self.BASE, 300.0]}],
        ) == []

        # ...while the ordinary factor of 1.0 is no obstacle.
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "meter", "factor": 1.0}},
            ],
            [{"metrics": [self.BASE, 300.0]}],
        )
        assert series[0][1] == 300.0

    def test_a_bad_unit_falls_back_to_the_gps_column(self):
        """The preferred column declaring an unusable unit drops that column,
        not the whole series — which is what the warning now says."""
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "furlong"}},
                {"key": "directGpsElevation", "metricsIndex": 2,
                 "unit": {"key": "meter"}},
            ],
            [{"metrics": [self.BASE, 1000.0, 305.0]}],
        )
        assert series == [(self.BASE / 1000.0, 305.0)]

    def test_elevation_sharing_the_timestamp_index_is_refused(self):
        # Reading the timestamp column as elevation yields ~1.7e12 m.
        assert self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 0},
            ],
            [{"metrics": [self.BASE]}],
        ) == []

    def test_samples_off_the_planet_are_dropped_individually(self):
        series = self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1},
            ],
            [
                {"metrics": [self.BASE, 300.0]},
                {"metrics": [self.BASE + 1_000, 1.7e12]},     # timestamp-ish
                {"metrics": [self.BASE + 2_000, -20_000.0]},  # below the floor
                {"metrics": [self.BASE + 3_000, 310.0]},
            ],
        )
        assert [s[1] for s in series] == [300.0, 310.0]

    def test_non_finite_values_are_not_samples(self):
        """``json.loads`` accepts the bare Infinity and NaN literals, SQLite
        stores inf in a REAL column, and a NaN timestamp would corrupt the
        sort the bisect lookup depends on."""
        assert self._series(
            [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directElevation", "metricsIndex": 1},
            ],
            [
                {"metrics": [self.BASE, float("inf")]},
                {"metrics": [float("nan"), 300.0]},
                {"metrics": [self.BASE + 1_000, float("nan")]},
            ],
        ) == []

    def test_malformed_descriptors_are_skipped(self):
        series = self._series(
            [
                "not a descriptor",
                {"key": "directTimestamp", "metricsIndex": 0},
                # True is an int in Python, and index 1 is the elevation.
                {"key": "directElevation", "metricsIndex": True},
                {"key": "directElevation", "metricsIndex": -1},
                {"key": "directElevation", "metricsIndex": 1.5},
                {"key": "directGpsElevation", "metricsIndex": 1},
            ],
            [{"metrics": [self.BASE, 305.0]}],
        )
        assert series == [(self.BASE / 1000.0, 305.0)]

    def test_no_timestamp_column_means_nothing_to_join_on(self):
        assert self._series(
            [{"key": "directElevation", "metricsIndex": 1}],
            [{"metrics": [self.BASE, 300.0]}],
        ) == []

    def test_rows_are_sorted_and_unusable_ones_skipped(self):
        descriptors = [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directElevation", "metricsIndex": 1},
        ]
        series = self._series(descriptors, [
            {"metrics": [self.BASE + 20_000, 320.0]},
            {"metrics": [self.BASE, None]},            # gap in the series
            {"metrics": [self.BASE + 10_000]},         # short row
            {"metrics": [self.BASE + 5_000, 305.0]},
            "not a row",
        ])
        assert series == [
            ((self.BASE + 5_000) / 1000.0, 305.0),
            ((self.BASE + 20_000) / 1000.0, 320.0),
        ]

    def test_missing_metrics_half_is_empty(self):
        assert igt.parse_elevation_series({}) == []
        assert igt.parse_elevation_series(None) == []
        assert igt.parse_elevation_series(
            {"metricDescriptors": "nope", "activityDetailMetrics": []}
        ) == []


class TestNearestElevation:
    def test_picks_the_closer_of_two_neighbours(self):
        series = [(100.0, 10.0), (110.0, 20.0)]
        assert igt.nearest_elevation(series, 104.0) == 10.0
        assert igt.nearest_elevation(series, 106.0) == 20.0

    def test_tolerance_is_inclusive_and_bounded(self):
        series = [(100.0, 10.0)]
        assert igt.nearest_elevation(series, 115.0, tolerance=15.0) == 10.0
        assert igt.nearest_elevation(series, 115.1, tolerance=15.0) is None

    def test_before_and_after_the_series(self):
        series = [(100.0, 10.0), (200.0, 20.0)]
        assert igt.nearest_elevation(series, 95.0) == 10.0
        assert igt.nearest_elevation(series, 205.0) == 20.0
        assert igt.nearest_elevation(series, 150.0) is None   # in the gap

    def test_an_exact_tie_goes_to_the_later_sample(self):
        # Arbitrary but fixed, so a future <= edit is visible.
        assert igt.nearest_elevation([(90.0, 1.0), (110.0, 2.0)], 100.0) == 2.0

    def test_an_exact_match_is_taken(self):
        assert igt.nearest_elevation([(100.0, 10.0), (110.0, 20.0)], 100.0) == 10.0

    def test_empty_series(self):
        assert igt.nearest_elevation([], 100.0) is None


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

    def test_load_native_excludes_declared_wifi_zone_points(self, loc_db):
        """A wifi-zone row is a coordinate the phone declares, not a fix it
        took, so it is not coverage and must not shadow a watch track
        (ISSUE-348)."""
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:00Z", 34.0, -118.0,
                                    accuracy=1.0, speed=0.0, source="overland",
                                    wifi_zone=True)             # declared
            location_db.insert_ping(conn, "2026-07-08T10:00:20Z", 34.0, -118.0,
                                    accuracy=8.0, source="overland")  # measured
            conn.commit()
            native = igt.load_native_points(
                conn, "2026-07-08T10:00:00Z", "2026-07-08T10:01:00Z", 300.0,
            )
        assert [n[0] for n in native] == [igt.parse_ts("2026-07-08T10:00:20Z")]

    def test_a_parked_phone_no_longer_clips_the_ends_of_a_run(self, loc_db):
        """The ISSUE-348 shape end to end: the phone sits at home emitting
        declared points for the whole activity, and the run starts and
        finishes at that same spot. Every point must survive."""
        home = (34.05000, -118.25000)
        # Out and back along one line, passing within metres of home at both
        # ends — well inside the 150 m guard radius.
        pts = [
            _tp("2026-07-08T10:00:00Z", home[0], home[1]),
            _tp("2026-07-08T10:05:00Z", home[0] + 0.02, home[1]),
            _tp("2026-07-08T10:10:00Z", home[0], home[1]),
        ]
        span = ("2026-07-08T10:00:00Z", "2026-07-08T10:10:00Z")
        with location_db.connect(loc_db) as conn:
            for i in range(11):
                location_db.insert_ping(
                    conn, f"2026-07-08T10:{i:02d}:00Z", home[0], home[1],
                    accuracy=1.0, speed=0.0, source="overland", wifi_zone=True,
                )
            conn.commit()
            native = igt.load_native_points(conn, *span, 300.0)
        assert native == []
        assert igt.filter_shadowed(pts, native, 300.0, 150.0) == pts

    def test_a_moving_phone_still_shadows_the_watch(self, loc_db):
        """The control for the two above: a real fix at the same coordinate
        shadows exactly as it did before, so the exclusion is about the
        marker and not about the position."""
        home = (34.05000, -118.25000)
        pts = [_tp("2026-07-08T10:00:00Z", home[0], home[1])]
        span = ("2026-07-08T10:00:00Z", "2026-07-08T10:00:00Z")
        with location_db.connect(loc_db) as conn:
            location_db.insert_ping(conn, "2026-07-08T10:00:05Z", home[0], home[1],
                                    accuracy=8.0, source="overland")
            conn.commit()
            native = igt.load_native_points(conn, *span, 300.0)
        assert len(native) == 1
        assert igt.filter_shadowed(pts, native, 300.0, 150.0) == []

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


class TestElevationReachesTheColumn:
    """ISSUE-488 end to end. The reported symptom was rows in
    ``location_pings`` with ``altitude IS NULL`` for every Garmin-imported
    point, so the assertion is on the column rather than on the parser."""

    def test_parsed_track_lands_with_altitude(self, loc_db):
        base = 1_700_000_000_000
        details = {
            "geoPolylineDTO": {"polyline": [
                # The watch signature: a 10s cadence and a null altitude.
                {"lat": 34.05 + i * 1e-4, "lon": -118.30, "altitude": None,
                 "speed": 2.5, "time": base + i * 10_000}
                for i in range(6)
            ]},
            "metricDescriptors": [
                {"key": "directTimestamp", "metricsIndex": 0,
                 "unit": {"key": "gmt"}},
                {"key": "directElevation", "metricsIndex": 1,
                 "unit": {"key": "meter"}},
            ],
            # Sampled on its own grid, offset from the polyline's.
            "activityDetailMetrics": [
                {"metrics": [base + i * 7_000, 300.0 + i * 5.0]}
                for i in range(9)
            ],
        }
        pts = igt.downsample(igt.parse_polyline(details, "hiking"), 10.0)
        assert pts
        with location_db.connect(loc_db) as conn:
            igt.insert_points(conn, pts)
            conn.commit()
            rows = conn.execute(
                "SELECT altitude FROM location_pings WHERE source='garmin' "
                "ORDER BY timestamp"
            ).fetchall()
        assert len(rows) == len(pts)
        assert all(r["altitude"] is not None for r in rows)
        assert rows[0]["altitude"] == 300.0


class TestTheJoinIsObservable:
    """Every way of getting the unverified metrics shape wrong fails closed,
    which looks exactly like the bug. So a track that came back without
    elevation has to say so above DEBUG and in the import report, or the
    first real run cannot tell a working fix from a silent no-op."""

    ACT = {
        "activityId": 9001,
        "hasPolyline": True,
        "startTimeGMT": "2023-11-14 22:13:20",
        "duration": 60.0,
    }

    class _Adapter:
        def __init__(self, details):
            self.details = details
            self.seen = {}

        def get_activity_details(self, activity_id, *, maxpoly, maxchart):
            self.seen = {"id": activity_id, "maxpoly": maxpoly,
                         "maxchart": maxchart}
            return self.details

    def _polyline(self, base, n=3):
        return [
            {"lat": 34.0 + i * 1e-4, "lon": -118.0, "altitude": None,
             "time": base + i * 10_000}
            for i in range(n)
        ]

    def test_a_track_with_no_elevation_warns(self, caplog):
        base = 1_700_000_000_000
        adapter = self._Adapter({
            "geoPolylineDTO": {"polyline": self._polyline(base)},
        })
        with caplog.at_level("WARNING", logger="istota.location.garmin_import"):
            pts, span = igt._fetch_points(adapter, self.ACT, igt.ImportOptions())
        assert pts and span
        assert all(p.altitude is None for p in pts)
        assert "9001" in caplog.text
        assert "no altitude" in caplog.text
        assert "0 usable elevation samples" in caplog.text

    def test_a_track_with_elevation_is_quiet(self, caplog):
        base = 1_700_000_000_000
        adapter = self._Adapter(_metrics_details(
            self._polyline(base),
            rows=[(base + i * 5_000, 300.0 + i) for i in range(13)],
        ))
        with caplog.at_level("WARNING", logger="istota.location.garmin_import"):
            pts, _ = igt._fetch_points(adapter, self.ACT, igt.ImportOptions())
        assert pts and all(p.altitude is not None for p in pts)
        assert caplog.text == ""

    def test_the_metrics_half_is_asked_for_at_the_polyline_cap(self):
        base = 1_700_000_000_000
        adapter = self._Adapter({
            "geoPolylineDTO": {"polyline": self._polyline(base)},
        })
        igt._fetch_points(adapter, self.ACT, igt.ImportOptions(maxpoly=4000))
        assert adapter.seen == {"id": "9001", "maxpoly": 4000, "maxchart": 4000}

    def test_the_report_row_counts_points_without_altitude(self, loc_db):
        base = 1_700_000_000_000
        adapter = self._Adapter({
            "geoPolylineDTO": {"polyline": self._polyline(base)},
        })
        with location_db.connect(loc_db) as conn:
            row = igt._process_activity(
                adapter, self.ACT, conn, igt.ImportOptions(), [], write=True,
            )
            conn.commit()
        assert row["no_altitude"] == row["inserted"] > 0

        adapter = self._Adapter(_metrics_details(
            self._polyline(base),
            rows=[(base + i * 5_000, 300.0 + i) for i in range(13)],
        ))
        with location_db.connect(loc_db) as conn:
            row = igt._process_activity(
                adapter, self.ACT, conn, igt.ImportOptions(), [], write=True,
            )
            conn.commit()
        assert row["inserted"] > 0
        assert row["no_altitude"] == 0
