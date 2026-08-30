"""A parked phone must not fill the table with the same declared point
(ISSUE-349).

While the device is on the configured home SSID the iOS shell stops reporting
fixes and reports a *declared* coordinate instead — a constant it asserts,
marked ``wifi_zone: true`` (ISSUE-229). It paced that at one point a minute and
never stopped: measured over one month of production rows, 26,713 of 39,319
native pings were declared points, all at a single coordinate, 99.4% of them
under 70 seconds apart, with one stay running 2,326 points across 42 hours.

The client now ramps — a few points to establish the stay, then a slow
keepalive — but a released build stays on the phone for at least a TestFlight
cycle, so the rule has to hold on the server too. That is the same division
ISSUE-229 settled: the server scrub is the fix, the shell change is hygiene.

What the server may not do is break the two things a stay is *for*. The visit
state machine opens on ``HYSTERESIS_THRESHOLD`` consecutive pings at a place,
and ``reconcile_visits`` discards a segment of fewer than ``min_pings`` or
shorter than ``min_dwell_sec`` as a walk-by. So the establishing points are
load-bearing and every test below that lets one through is guarding a
regression that would silently delete visits rather than duplicate rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota.location import db as location_db

pytest.importorskip("fastapi", reason="fastapi not installed")


def _init_db(tmp_path: Path) -> Path:
    path = tmp_path / "location.db"
    location_db.init_db(path)
    return path


ZONE_LON, ZONE_LAT = -118.25000, 34.05000


def _zone_feature(ts: str, lon: float = ZONE_LON, lat: float = ZONE_LAT) -> dict:
    """What ``wifiZoneLocation`` + ``featureFromLocation:wifiZone:`` emit."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "timestamp": ts,
            "altitude": -1,
            "speed": 0,
            "course": 0,
            "horizontal_accuracy": 1,
            "vertical_accuracy": 0,
            "wifi_zone": True,
            "wifi": "home-ssid",
            "battery_level": 0.9,
            "motion": ["stationary"],
            "client_id": ts,          # the shell mints a fresh UUID per point
        },
    }


def _measured_feature(ts: str, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "timestamp": ts,
            "horizontal_accuracy": 8,
            "speed": 3.1,
            "battery_level": 0.9,
            "motion": ["walking"],
            "client_id": ts,
        },
    }


def _feed(path: Path, features: list[dict]) -> list[dict]:
    from istota.webhook_receiver import _process_feature

    with location_db.connect(path) as conn:
        for f in features:
            _process_feature(conn, f, [])
        conn.commit()
        return [
            dict(r) for r in conn.execute(
                "SELECT timestamp, lat, lon, wifi_zone FROM location_pings "
                "ORDER BY timestamp"
            )
        ]


def _minutes(n: int) -> str:
    return f"2026-08-30T{10 + n // 60:02d}:{n % 60:02d}:00Z"


class TestTheEstablishingPointsSurvive:
    """The half that must never regress."""

    def test_the_first_three_declared_points_are_all_stored(self, tmp_path):
        path = _init_db(tmp_path)
        rows = _feed(path, [_zone_feature(_minutes(n)) for n in range(3)])
        assert len(rows) == 3

    def test_they_span_the_reconciler_dwell_floor(self, tmp_path):
        """Three points a minute apart is 120 s, clearing min_dwell_sec=60."""
        path = _init_db(tmp_path)
        rows = _feed(path, [_zone_feature(_minutes(n)) for n in range(3)])
        assert rows[0]["timestamp"] == "2026-08-30T10:00:00Z"
        assert rows[-1]["timestamp"] == "2026-08-30T10:02:00Z"

    def test_a_short_stay_is_not_thinned_below_the_walk_by_floor(self, tmp_path):
        """reconcile_visits drops a segment of fewer than 3 pings. A ten-minute
        stay must therefore still arrive as at least 3."""
        path = _init_db(tmp_path)
        rows = _feed(path, [_zone_feature(_minutes(n)) for n in range(10)])
        assert len(rows) >= 3

    def test_returning_home_establishes_a_fresh_stay(self, tmp_path):
        """Real fixes in between break the run, so the next arrival gets its
        own establishing points rather than inheriting the last stay's."""
        path = _init_db(tmp_path)
        away = [_measured_feature(_minutes(n), -118.2 + n / 1000, 34.2)
                for n in range(3, 6)]
        rows = _feed(
            path,
            [_zone_feature(_minutes(n)) for n in range(3)]
            + away
            + [_zone_feature(_minutes(n)) for n in range(6, 9)],
        )
        declared = [r for r in rows if r["wifi_zone"]]
        assert len(declared) == 6


class TestTheKeepalive:
    """The half that fixes the flood."""

    def test_a_long_parked_stay_collapses(self, tmp_path):
        """Twelve hours at one point a minute is 720 rows before the fix."""
        path = _init_db(tmp_path)
        rows = _feed(path, [_zone_feature(_minutes(n)) for n in range(720)])
        assert len(rows) < 60

    def test_the_keepalive_still_reaches_the_whole_stay(self, tmp_path):
        """Thinning is not truncation — the last stored point is near the end
        of the stay, so the live views and the day's battery reading do not
        stop twelve hours early."""
        path = _init_db(tmp_path)
        rows = _feed(path, [_zone_feature(_minutes(n)) for n in range(720)])
        assert rows[-1]["timestamp"] >= "2026-08-30T21:00:00Z"

    def test_a_measured_point_is_never_suppressed(self, tmp_path):
        """The control. The rule is about the marker, not about the position:
        a real fix repeating a coordinate is still a measurement."""
        path = _init_db(tmp_path)
        rows = _feed(
            path,
            [_measured_feature(_minutes(n), ZONE_LON, ZONE_LAT) for n in range(20)],
        )
        assert len(rows) == 20

    def test_a_declared_point_that_moves_is_never_suppressed(self, tmp_path):
        """A reconfigured zone is a different assertion and has to land."""
        path = _init_db(tmp_path)
        rows = _feed(
            path,
            [_zone_feature(_minutes(n)) for n in range(3)]
            + [_zone_feature(_minutes(n), lon=-118.4, lat=34.5)
               for n in range(3, 6)],
        )
        assert len(rows) == 6
