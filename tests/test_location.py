"""Tests for location tracking: loader, DB functions, haversine, state machine, CLI."""

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    import geopy  # noqa: F401
    _has_geopy = True
except ImportError:
    _has_geopy = False

try:
    import fastapi  # noqa: F401
    _has_fastapi = True
except ImportError:
    _has_fastapi = False

_needs_geopy = pytest.mark.skipif(not _has_geopy, reason="geopy not installed")
_needs_fastapi = pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")

from istota import db
from istota.config import Config, UserConfig
from istota.geo import haversine

if _has_fastapi:
    from istota.webhook_receiver import resolve_place


def _init_db(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


# ===========================================================================
# DB function tests
# ===========================================================================


class TestLocationPingDB:
    def test_insert_and_get_latest(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_location_ping(
                conn, "alice", "2026-02-20T10:00:00Z", 34.05, -118.4,
                accuracy=5.0, activity_type="stationary",
            )
            db.insert_location_ping(
                conn, "alice", "2026-02-20T10:05:00Z", 34.06, -118.3,
                accuracy=10.0, speed=3.0,
            )
            conn.commit()

            latest = db.get_latest_ping(conn, "alice")
            assert latest is not None
            assert latest.lat == 34.06
            assert latest.timestamp == "2026-02-20T10:05:00Z"

    def test_get_latest_no_pings(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.get_latest_ping(conn, "alice") is None

    def test_get_pings_with_filters(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_location_ping(conn, "alice", "2026-02-20T08:00:00Z", 1.0, 2.0)
            db.insert_location_ping(conn, "alice", "2026-02-20T12:00:00Z", 3.0, 4.0)
            db.insert_location_ping(conn, "alice", "2026-02-20T16:00:00Z", 5.0, 6.0)
            conn.commit()

            # Since filter
            pings = db.get_pings(conn, "alice", since="2026-02-20T10:00:00Z")
            assert len(pings) == 2

            # Until filter
            pings = db.get_pings(conn, "alice", until="2026-02-20T13:00:00Z")
            assert len(pings) == 2

            # Limit
            pings = db.get_pings(conn, "alice", limit=1)
            assert len(pings) == 1
            assert pings[0].lat == 5.0  # newest first

    def test_batch_insert(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            count = db.insert_location_pings_batch(conn, [
                {"user_id": "alice", "timestamp": "2026-01-01T00:00:00Z", "lat": 1.0, "lon": 2.0},
                {"user_id": "alice", "timestamp": "2026-01-01T00:01:00Z", "lat": 3.0, "lon": 4.0},
            ])
            assert count == 2
            assert len(db.get_pings(conn, "alice")) == 2


class TestPlaceDB:
    def test_crud(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0, 150, "home")
            assert pid > 0

            places = db.get_places(conn, "alice")
            assert len(places) == 1
            assert places[0].name == "home"

            place = db.get_place_by_name(conn, "alice", "home")
            assert place is not None
            assert place.radius_meters == 150

            assert db.delete_place(conn, "alice", "home")
            assert db.get_places(conn, "alice") == []

    def test_upsert(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            id1 = db.upsert_place(conn, "alice", "home", 1.0, 2.0, 100)
            id2 = db.upsert_place(conn, "alice", "home", 3.0, 4.0, 200)
            assert id1 == id2
            place = db.get_place_by_name(conn, "alice", "home")
            assert place.lat == 3.0
            assert place.radius_meters == 200


class TestVisitDB:
    def test_insert_and_close(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            vid = db.insert_visit(conn, "alice", pid, "home", "2026-02-20T08:00:00")
            conn.commit()

            visit = db.get_open_visit(conn, "alice")
            assert visit is not None
            assert visit.place_name == "home"
            assert visit.exited_at is None

            db.close_visit(conn, vid, "2026-02-20T10:00:00")
            conn.commit()

            assert db.get_open_visit(conn, "alice") is None

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].exited_at == "2026-02-20T10:00:00"
            assert visits[0].duration_sec > 0

    def test_increment_ping_count(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            vid = db.insert_visit(conn, "alice", None, "unknown", "2026-02-20T08:00:00")
            db.increment_visit_ping_count(conn, vid)
            db.increment_visit_ping_count(conn, vid)
            conn.commit()

            visit = db.get_open_visit(conn, "alice")
            assert visit.ping_count == 3  # 1 initial + 2 increments


@_needs_fastapi
class TestPlaceStats:
    def _add_pings(self, conn, user_id, place_id, timestamps):
        """Insert pings at a place for given ISO timestamps."""
        for ts in timestamps:
            db.insert_location_ping(
                conn, user_id, ts, 34.0, -118.0,
                accuracy=5.0, activity_type="stationary",
            )
            # Assign place_id to the ping we just inserted
            last_id = conn.execute("SELECT max(id) FROM location_pings").fetchone()[0]
            conn.execute(
                "UPDATE location_pings SET place_id = ? WHERE id = ?",
                (place_id, last_id),
            )

    def test_no_pings(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0, 150, "home")
            conn.commit()

        result = _location_place_stats(str(db_path), "alice", pid)
        assert result is not None
        assert result["total_visits"] == 0
        assert result["first_visit"] is None

    def test_single_visit_from_pings(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            # Pings 5 min apart = one visit spanning 1 hour
            self._add_pings(conn, "alice", pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:30:00Z",
                "2026-01-10T10:00:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), "alice", pid)
        assert result["total_visits"] == 1
        assert result["avg_duration_min"] == 60
        assert result["total_duration_min"] == 60

    def test_gap_without_elsewhere_is_same_visit(self, tmp_path):
        """A long gap with no pings at other places should NOT split the visit."""
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            # Pings with a 2-hour gap (GPS dropout indoors)
            self._add_pings(conn, "alice", pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                # 2-hour gap — no pings elsewhere
                "2026-01-10T11:10:00Z",
                "2026-01-10T11:15:00Z",
                "2026-01-10T11:20:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), "alice", pid)
        assert result["total_visits"] == 1
        assert result["total_duration_min"] == 140  # 09:00 to 11:20

    def test_two_visits_split_by_elsewhere(self, tmp_path):
        """Pings at another place during a gap should split into two visits."""
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_cafe = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            pid_gym = db.insert_place(conn, "alice", "gym", 34.01, -118.01, 100, "gym")
            # Visit 1 at cafe
            self._add_pings(conn, "alice", pid_cafe, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                "2026-01-10T09:15:00Z",
                "2026-01-10T09:20:00Z",
            ])
            # Went to gym in between
            self._add_pings(conn, "alice", pid_gym, [
                "2026-01-10T10:00:00Z",
                "2026-01-10T10:05:00Z",
            ])
            # Visit 2 at cafe
            self._add_pings(conn, "alice", pid_cafe, [
                "2026-01-10T11:20:00Z",
                "2026-01-10T11:25:00Z",
                "2026-01-10T11:30:00Z",
                "2026-01-10T11:35:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), "alice", pid_cafe)
        assert result["total_visits"] == 2
        assert result["first_visit"] == "2026-01-10T09:00:00Z"
        assert result["last_visit"] == "2026-01-10T11:20:00Z"

    def test_walkby_filtered(self, tmp_path):
        """A visit with fewer than 3 pings (walk-by) should not count."""
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            # Only 2 pings — just passing by
            self._add_pings(conn, "alice", pid, [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
            ])
            conn.commit()

        result = _location_place_stats(str(db_path), "alice", pid)
        assert result["total_visits"] == 0

    def test_wrong_user_returns_none(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            conn.commit()

        result = _location_place_stats(str(db_path), "bob", pid)
        assert result is None

    def test_nonexistent_place_returns_none(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_db(tmp_path)
        result = _location_place_stats(str(db_path), "alice", 9999)
        assert result is None


@_needs_fastapi
class TestPlaceUpdateReassignment:
    def test_move_place_reassigns_pings(self, tmp_path):
        """Moving a place center should reassign pings to match the new geofence."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            # Place at 34.0, -118.0
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 50, "food")
            # Pings at 34.0001, -118.0 (~11m from place) — within 50m radius
            for i, ts in enumerate([
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
                "2026-01-10T09:15:00Z",
            ]):
                db.insert_location_ping(conn, "alice", ts, 34.0001, -118.0, accuracy=5.0)
                last_id = conn.execute("SELECT max(id) FROM location_pings").fetchone()[0]
                conn.execute("UPDATE location_pings SET place_id = ? WHERE id = ?", (pid, last_id))

            # Pings at 34.001, -118.0 (~111m from place) — outside 50m radius
            for ts in [
                "2026-02-10T10:00:00Z",
                "2026-02-10T10:05:00Z",
                "2026-02-10T10:10:00Z",
            ]:
                db.insert_location_ping(conn, "alice", ts, 34.001, -118.0, accuracy=5.0)
            conn.commit()

        # Before move: 4 pings assigned, stats should show 1 visit
        stats = _location_place_stats(str(db_path), "alice", pid)
        assert stats["total_visits"] == 1

        # Move place to 34.001, -118.0 (near the unassigned pings)
        _location_update_place(str(db_path), "alice", pid, {"lat": 34.001, "lon": -118.0})

        # After move: old pings should be unassigned, new pings assigned
        stats = _location_place_stats(str(db_path), "alice", pid)
        assert stats["total_visits"] == 1
        assert stats["first_visit"] == "2026-02-10T10:00:00Z"

    def test_radius_change_reassigns_pings(self, tmp_path):
        """Expanding radius should pick up nearby unassigned pings."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 25, "food")
            # Pings at ~40m away — outside 25m but inside 100m
            for ts in [
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:05:00Z",
                "2026-01-10T09:10:00Z",
            ]:
                db.insert_location_ping(conn, "alice", ts, 34.00035, -118.0, accuracy=5.0)
            conn.commit()

        # Before: no pings assigned
        stats = _location_place_stats(str(db_path), "alice", pid)
        assert stats["total_visits"] == 0

        # Expand radius to 100m
        _location_update_place(str(db_path), "alice", pid, {"radius_meters": 100})

        # After: pings should now be assigned
        stats = _location_place_stats(str(db_path), "alice", pid)
        assert stats["total_visits"] == 1


class TestLocationStateDB:
    def test_get_set(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.get_location_state(conn, "alice") is None

            db.set_location_state(conn, "alice", 1, 2, 3, 4)
            conn.commit()

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == 1
            assert state.current_visit_id == 2
            assert state.consecutive_count == 3
            assert state.last_ping_place_id == 4

            # Upsert
            db.set_location_state(conn, "alice", 5, 6, 0, None)
            conn.commit()

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == 5
            assert state.last_ping_place_id is None


# ===========================================================================
# Haversine + place resolution tests
# ===========================================================================


class TestHaversine:
    def test_same_point(self):
        assert haversine(34.0, -118.0, 34.0, -118.0) == 0.0

    def test_known_distance(self):
        # NYC to LA ~ 3944 km
        dist = haversine(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3930_000 < dist < 3960_000

    def test_short_distance(self):
        # ~111 m per 0.001 degree latitude
        dist = haversine(34.000, -118.0, 34.001, -118.0)
        assert 100 < dist < 120


@_needs_fastapi
class TestResolvePlace:
    def test_within_radius(self):
        places = [
            db.Place(1, "alice", "home", 34.0, -118.0, 200, "home", "", None),
        ]
        result = resolve_place(34.0001, -118.0001, places)
        assert result is not None
        assert result.name == "home"

    def test_outside_radius(self):
        places = [
            db.Place(1, "alice", "home", 34.0, -118.0, 50, "home", "", None),
        ]
        result = resolve_place(35.0, -119.0, places)
        assert result is None

    def test_nearest_wins(self):
        places = [
            db.Place(1, "alice", "far", 34.01, -118.0, 5000, "other", "", None),
            db.Place(2, "alice", "near", 34.0001, -118.0001, 5000, "other", "", None),
        ]
        result = resolve_place(34.0, -118.0, places)
        assert result.name == "near"

    def test_empty_places(self):
        assert resolve_place(34.0, -118.0, []) is None


# ===========================================================================
# State machine tests
# ===========================================================================


@_needs_fastapi
class TestStateMachine:
    """Tests for the state machine logic in webhook_receiver."""

    def _process(self, conn, user_id, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = db.insert_location_ping(
            conn, user_id, timestamp, 0.0, 0.0,
        )
        _update_state_machine(
            conn, user_id, ping_id, place_id, place, timestamp,
        )
        return ping_id

    def test_first_ping_at_place(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            place = db.get_place_by_name(conn, "alice", "home")

            self._process(conn, "alice", pid, place, "2026-02-20T10:00:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == pid
            assert state.current_visit_id is not None

            visit = db.get_open_visit(conn, "alice")
            assert visit.place_name == "home"

    def test_first_ping_no_place(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            self._process(conn, "alice", None, None, "2026-02-20T10:00:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id is None
            assert state.current_visit_id is None

    def test_same_place_no_transition(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            place = db.get_place_by_name(conn, "alice", "home")

            self._process(conn, "alice", pid, place, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid, place, "2026-02-20T10:05:00Z")
            self._process(conn, "alice", pid, place, "2026-02-20T10:10:00Z")

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1  # still one visit
            assert visits[0].ping_count == 3

    def test_hysteresis_prevents_single_ping_transition(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_home = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_gym = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            home = db.get_place_by_name(conn, "alice", "home")
            gym = db.get_place_by_name(conn, "alice", "gym")

            # Establish at home
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:05:00Z")

            # Single ping at gym — should NOT transition (hysteresis)
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:10:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == pid_home  # still at home
            assert state.consecutive_count == 1

    def test_hysteresis_allows_transition_after_threshold(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_home = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_gym = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            home = db.get_place_by_name(conn, "alice", "home")
            gym = db.get_place_by_name(conn, "alice", "gym")

            # Establish at home
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:05:00Z")

            # Two consecutive pings at gym — should transition
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:10:00Z")
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:15:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == pid_gym

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 2
            # Home visit should be closed
            home_visit = [v for v in visits if v.place_name == "home"][0]
            assert home_visit.exited_at is not None
            # Gym visit should be open
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert gym_visit.exited_at is None

    def test_transition_from_place_to_unknown(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_home = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            home = db.get_place_by_name(conn, "alice", "home")

            self._process(conn, "alice", pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:05:00Z")

            # Two pings at unknown
            self._process(conn, "alice", None, None, "2026-02-20T10:10:00Z")
            self._process(conn, "alice", None, None, "2026-02-20T10:15:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id is None

    def test_transition_fires_without_errors(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_home = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_gym = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            home = db.get_place_by_name(conn, "alice", "home")
            gym = db.get_place_by_name(conn, "alice", "gym")

            # Establish at home
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:05:00Z")

            # Transition to gym
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:10:00Z")
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:15:00Z")

            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == pid_gym


# ===========================================================================
# Overland payload parsing tests
# ===========================================================================


@_needs_fastapi
class TestOverlandPayloadParsing:
    """Test that the receiver correctly parses Overland GeoJSON payloads."""

    def test_parse_feature_coordinates(self):
        """Verify coordinate extraction from GeoJSON Feature."""
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [-122.030581, 37.331800],
            },
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

        db_path = _init_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with db.get_db(db_path) as conn:
            _process_feature(conn, "alice", feature, [])
            conn.commit()

            pings = db.get_pings(conn, "alice")
            assert len(pings) == 1
            p = pings[0]
            # GeoJSON: coordinates = [lon, lat]
            assert p.lon == -122.030581
            assert p.lat == 37.331800
            assert p.accuracy == 5
            assert p.activity_type == "stationary"
            assert p.battery == 0.92
            assert p.wifi == "home-wifi"

    def test_parse_negative_speed_becomes_none(self):
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {
                "timestamp": "2026-01-01T00:00:00Z",
                "speed": -1,
                "course": -1,
            },
        }

        db_path = _init_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with db.get_db(db_path) as conn:
            _process_feature(conn, "alice", feature, [])
            conn.commit()

            p = db.get_latest_ping(conn, "alice")
            assert p.speed is None
            assert p.course is None

    def test_feature_with_activity_string(self):
        """Overland can send activity as a string instead of motion array."""
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {
                "timestamp": "2026-01-01T00:00:00Z",
                "activity": "other_navigation",
            },
        }

        db_path = _init_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with db.get_db(db_path) as conn:
            _process_feature(conn, "alice", feature, [])
            conn.commit()

            p = db.get_latest_ping(conn, "alice")
            assert p.activity_type == "other_navigation"

    def test_empty_coordinates_skipped(self):
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": []},
            "properties": {"timestamp": "2026-01-01T00:00:00Z"},
        }

        db_path = _init_db(Path(pytest.importorskip("tempfile").mkdtemp()))
        with db.get_db(db_path) as conn:
            _process_feature(conn, "alice", feature, [])
            conn.commit()

            assert db.get_latest_ping(conn, "alice") is None


# ===========================================================================
# CLI tests
# ===========================================================================


class TestLocationCLI:
    def test_current_no_data(self, tmp_path):
        db_path = _init_db(tmp_path)
        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_current
            import io
            from unittest.mock import MagicMock

            args = MagicMock()
            import sys
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_current(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["last_ping"] is None
            assert output["current_visit"] is None

    def test_places_lists_db(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "home", 34.0, -118.0, 150, "home")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_places
            import io, sys
            from unittest.mock import MagicMock

            args = MagicMock()
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_places(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 1
            assert output[0]["name"] == "home"
            assert output[0]["radius_meters"] == 150

    def test_places_includes_id(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 34.0, -118.0, 150, "home")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_places

            args = MagicMock()
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_places(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output[0]["id"] == pid

    def test_update_by_name(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "restaurant")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["place"]["category"] == "food"
            assert output["place"]["name"] == "cafe"

        # Verify DB
        with db.get_db(db_path) as conn:
            place = db.get_place_by_name(conn, "alice", "cafe")
            assert place.category == "food"

    def test_update_by_id(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "restaurant")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = None
            args.id = pid
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["place"]["category"] == "food"

    def test_update_rename(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "old name", 34.0, -118.0, 100, "other")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "old name"
            args.id = None
            args.rename = "new name"
            args.category = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["place"]["name"] == "new name"

        with db.get_db(db_path) as conn:
            assert db.get_place_by_name(conn, "alice", "new name") is not None
            assert db.get_place_by_name(conn, "alice", "old name") is None

    def test_update_not_found(self, tmp_path):
        db_path = _init_db(tmp_path)

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "nonexistent"
            args.id = None
            args.category = "food"
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_update_no_changes(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_update

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            args.category = None
            args.rename = None
            args.radius = None
            args.notes = None
            args.lat = None
            args.lon = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_update(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_delete_by_name(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = "cafe"
            args.id = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"
            assert output["deleted"] == "cafe"

        with db.get_db(db_path) as conn:
            assert db.get_place_by_name(conn, "alice", "cafe") is None

    def test_delete_by_id(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "cafe", 34.0, -118.0, 100, "food")
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = None
            args.id = pid
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert output["status"] == "ok"

        with db.get_db(db_path) as conn:
            assert db.get_places(conn, "alice") == []

    def test_delete_not_found(self, tmp_path):
        db_path = _init_db(tmp_path)

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_delete

            args = MagicMock()
            args.name = "nonexistent"
            args.id = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with pytest.raises(SystemExit):
                    cmd_delete(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert "error" in output

    def test_history_lists_pings(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_location_ping(
                conn, "alice", "2026-02-20T10:00:00Z", 34.0, -118.0,
                accuracy=5.0, activity_type="walking",
            )
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history
            import io, sys
            from unittest.mock import MagicMock

            args = MagicMock()
            args.limit = 10
            args.date = None
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 1
            assert output[0]["lat"] == 34.0

    def test_history_date_uses_timezone_aware_boundaries(self, tmp_path):
        """history --date should convert local day boundaries to UTC."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            # 2026-03-16 in Pacific = 2026-03-16T07:00:00Z to 2026-03-17T07:00:00Z (PDT)
            # Ping at 2026-03-16T02:00:00Z = Mar 15 7pm Pacific — outside Mar 16 local
            db.insert_location_ping(
                conn, "alice", "2026-03-16T02:00:00Z", 34.0, -118.0,
                accuracy=5.0, activity_type="stationary",
            )
            # Ping at 2026-03-16T20:00:00Z = Mar 16 1pm Pacific — inside Mar 16 local
            db.insert_location_ping(
                conn, "alice", "2026-03-16T20:00:00Z", 34.1, -118.1,
                accuracy=5.0, activity_type="walking",
            )
            # Ping at 2026-03-17T03:00:00Z = Mar 16 8pm Pacific — inside Mar 16 local
            db.insert_location_ping(
                conn, "alice", "2026-03-17T03:00:00Z", 34.2, -118.2,
                accuracy=5.0, activity_type="walking",
            )
            # Ping at 2026-03-17T10:00:00Z = Mar 17 3am Pacific — outside Mar 16 local
            db.insert_location_ping(
                conn, "alice", "2026-03-17T10:00:00Z", 34.3, -118.3,
                accuracy=5.0, activity_type="stationary",
            )
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 0
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            # Should only include the two pings within Mar 16 Pacific
            assert len(output) == 2
            lats = {p["lat"] for p in output}
            assert lats == {34.1, 34.2}

    def test_history_date_returns_all_pings_by_default(self, tmp_path):
        """history --date with no --limit should return all pings, not just 20."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            # Insert 30 pings spread across Mar 16 Pacific
            for i in range(30):
                ts = f"2026-03-16T{15 + (i // 6):02d}:{(i % 6) * 10:02d}:00Z"
                db.insert_location_ping(
                    conn, "alice", ts, 34.0 + i * 0.001, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 0
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 30

    def test_history_date_respects_explicit_limit(self, tmp_path):
        """history --date --limit N should cap results."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            for i in range(10):
                ts = f"2026-03-16T{15 + i}:00:00Z"
                db.insert_location_ping(
                    conn, "alice", ts, 34.0, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        with patch.dict("os.environ", env):
            from istota.skills.location import cmd_history

            args = MagicMock()
            args.limit = 5
            args.date = "2026-03-16"
            args.tz = "America/Los_Angeles"
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_history(args)
            finally:
                sys.stdout = old_stdout

            output = json.loads(captured.getvalue())
            assert len(output) == 5


# ===========================================================================
# Geocode cache DB tests
# ===========================================================================


class TestGeocodeCache:
    def test_cache_miss_returns_none(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            assert db.get_cached_geocode(conn, "123 Main St") is None

    def test_cache_and_retrieve(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            conn.commit()

            result = db.get_cached_geocode(conn, "123 Main St")
            assert result == (34.05, -118.4)

    def test_cache_upsert(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            db.cache_geocode(conn, "123 Main St", 35.0, -119.0)
            conn.commit()

            result = db.get_cached_geocode(conn, "123 Main St")
            assert result == (35.0, -119.0)


# ===========================================================================
# Attendance helper tests
# ===========================================================================


class TestVirtualLocationDetection:
    def test_zoom_link(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("https://zoom.us/j/12345") is True

    def test_google_meet(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("meet.google.com/abc-def") is True

    def test_teams(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("Microsoft Teams Meeting") is True

    def test_physical_location(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("123 Main St, San Francisco") is False

    def test_conference_room(self):
        from istota.skills.location import _is_virtual_location
        assert _is_virtual_location("Conference Room B") is False


class TestPlaceMatching:
    def test_exact_match(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("gym", places)
        assert result is not None
        assert result["name"] == "gym"

    def test_case_insensitive(self):
        from istota.skills.location import _match_place
        places = [{"name": "Downtown Gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("downtown gym", places)
        assert result is not None

    def test_substring_match_location_in_place(self):
        from istota.skills.location import _match_place
        places = [{"name": "Downtown Gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("gym", places)
        assert result is not None
        assert result["name"] == "Downtown Gym"

    def test_substring_match_place_in_location(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("The gym on 5th Ave", places)
        assert result is not None

    def test_no_match(self):
        from istota.skills.location import _match_place
        places = [{"name": "gym", "lat": 34.0, "lon": -118.0, "radius_meters": 100}]
        result = _match_place("dentist office", places)
        assert result is None

    def test_empty_places(self):
        from istota.skills.location import _match_place
        assert _match_place("gym", []) is None


class TestGeocodeLocation:
    def test_cache_hit(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            conn.commit()

            result = _geocode_location("123 Main St", conn)
            assert result == (34.05, -118.4)

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            mock_result = MagicMock()
            mock_result.latitude = 37.7749
            mock_result.longitude = -122.4194

            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.return_value = mock_result
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("San Francisco, CA", conn)
                assert result == (37.7749, -122.4194)

                # Should be cached now
                cached = db.get_cached_geocode(conn, "San Francisco, CA")
                assert cached == (37.7749, -122.4194)

    @_needs_geopy
    def test_nominatim_failure_returns_none(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.return_value = None
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("nonexistent place xyz", conn)
                assert result is None

    @_needs_geopy
    def test_nominatim_exception_returns_none(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.geocode.side_effect = Exception("timeout")
                mock_nom_cls.return_value = mock_geolocator

                result = _geocode_location("123 Main St", conn)
                assert result is None


# ===========================================================================
# Attendance command tests
# ===========================================================================


def _make_calendar_event(
    uid="ev1",
    summary="Meeting",
    start=None,
    end=None,
    location=None,
    all_day=False,
):
    """Create a mock CalendarEvent."""
    from istota.skills.calendar import CalendarEvent
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    if start is None:
        start = datetime(2026, 3, 1, 10, 0, tzinfo=tz)
    if end is None:
        end = datetime(2026, 3, 1, 11, 0, tzinfo=tz)
    return CalendarEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        location=location,
        all_day=all_day,
    )


class TestCmdAttendance:
    def _run_attendance(self, tmp_path, events, pings=None, places=None, args_overrides=None):
        """Helper to run cmd_attendance with mocked CalDAV and DB."""
        from istota.skills.location import cmd_attendance

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            # Insert places
            for p in (places or []):
                db.insert_place(conn, "alice", p["name"], p["lat"], p["lon"],
                                p.get("radius_meters", 100), p.get("category", "other"))
            # Insert pings
            for ping in (pings or []):
                db.insert_location_ping(
                    conn, "alice", ping["timestamp"], ping["lat"], ping["lon"],
                    accuracy=ping.get("accuracy", 5.0),
                )
            conn.commit()

        env = {
            "ISTOTA_DB_PATH": str(db_path),
            "ISTOTA_USER_ID": "alice",
            "CALDAV_URL": "https://cloud.example.com/remote.php/dav",
            "CALDAV_USERNAME": "alice",
            "CALDAV_PASSWORD": "secret",
            "TZ": "America/Los_Angeles",
        }

        args = MagicMock()
        args.date = "2026-03-01"
        args.event = None
        if args_overrides:
            for k, v in args_overrides.items():
                setattr(args, k, v)

        mock_client = MagicMock()
        mock_calendars = [("Personal", "https://cal.example.com/personal")]

        with patch.dict("os.environ", env):
            with patch("istota.skills.calendar.get_caldav_client", return_value=mock_client):
                with patch("istota.skills.calendar.list_calendars", return_value=mock_calendars):
                    with patch("istota.skills.calendar.get_events", return_value=events):
                        captured = io.StringIO()
                        old_stdout = sys.stdout
                        sys.stdout = captured
                        try:
                            cmd_attendance(args)
                        finally:
                            sys.stdout = old_stdout

        return json.loads(captured.getvalue())

    def test_no_events(self, tmp_path):
        result = self._run_attendance(tmp_path, events=[])
        assert result["date"] == "2026-03-01"
        assert result["events"] == []

    def test_all_day_event_filtered(self, tmp_path):
        events = [_make_calendar_event(location="123 Main St", all_day=True)]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_no_location_filtered(self, tmp_path):
        events = [_make_calendar_event(location=None)]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_virtual_location_filtered(self, tmp_path):
        events = [_make_calendar_event(location="https://zoom.us/j/12345")]
        result = self._run_attendance(tmp_path, events=events)
        assert result["events"] == []

    def test_attendance_confirmed_with_nearby_pings(self, tmp_path):
        events = [_make_calendar_event(
            uid="dentist1",
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200}]
        pings = [
            {"timestamp": "2026-03-01T17:45:00Z", "lat": 34.0501, "lon": -118.4001},  # 10:45 PT, within window
            {"timestamp": "2026-03-01T18:30:00Z", "lat": 34.0502, "lon": -118.3999},  # 11:30 PT, within window
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "place"
        assert ev["nearby_ping_count"] == 2

    def test_no_pings_no_attendance(self, tmp_path):
        events = [_make_calendar_event(
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200}]
        result = self._run_attendance(tmp_path, events=events, pings=[], places=places)
        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["attended"] is None

    def test_pings_too_far_away(self, tmp_path):
        events = [_make_calendar_event(
            summary="Dentist",
            location="dentist office",
        )]
        places = [{"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 100}]
        # Pings far from the dentist
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 35.0, "lon": -119.0},
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        ev = result["events"][0]
        assert ev["attended"] is None

    @_needs_geopy
    def test_ungeocoded_event(self, tmp_path):
        events = [_make_calendar_event(
            summary="Meeting",
            location="Some Unknown Place XYZ123",
        )]
        # No places, geocoding will fail
        with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.geocode.return_value = None
            mock_nom_cls.return_value = mock_geolocator

            result = self._run_attendance(tmp_path, events=events)

        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["location_resolved"] is False
        assert ev["attended"] is None

    @_needs_geopy
    def test_geocoded_event_with_attendance(self, tmp_path):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        events = [_make_calendar_event(
            summary="Dentist",
            location="123 Main St, LA",
            start=datetime(2026, 3, 1, 10, 0, tzinfo=tz),
            end=datetime(2026, 3, 1, 11, 0, tzinfo=tz),
        )]
        # Ping near geocoded location
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 34.0501, "lon": -118.4001},
        ]

        mock_result = MagicMock()
        mock_result.latitude = 34.05
        mock_result.longitude = -118.4

        with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.geocode.return_value = mock_result
            mock_nom_cls.return_value = mock_geolocator

            result = self._run_attendance(tmp_path, events=events, pings=pings)

        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "geocode"

    def test_event_filter_by_title(self, tmp_path):
        events = [
            _make_calendar_event(uid="ev1", summary="Dentist", location="dentist office"),
            _make_calendar_event(uid="ev2", summary="Gym", location="gym"),
        ]
        places = [
            {"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200},
            {"name": "gym", "lat": 34.1, "lon": -118.1, "radius_meters": 100},
        ]
        result = self._run_attendance(
            tmp_path, events=events, places=places,
            args_overrides={"event": "dentist"},
        )
        assert len(result["events"]) == 1
        assert result["events"][0]["summary"] == "Dentist"

    def test_event_filter_by_uid(self, tmp_path):
        events = [
            _make_calendar_event(uid="abc123", summary="Dentist", location="dentist office"),
            _make_calendar_event(uid="def456", summary="Gym", location="gym"),
        ]
        places = [
            {"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200},
            {"name": "gym", "lat": 34.1, "lon": -118.1, "radius_meters": 100},
        ]
        result = self._run_attendance(
            tmp_path, events=events, places=places,
            args_overrides={"event": "abc123"},
        )
        assert len(result["events"]) == 1
        assert result["events"][0]["uid"] == "abc123"

    def test_place_radius_used(self, tmp_path):
        """Place with large radius should detect pings that would be outside default 200m."""
        events = [_make_calendar_event(
            summary="Park",
            location="big park",
        )]
        # Place with 2km radius
        places = [{"name": "big park", "lat": 34.05, "lon": -118.4, "radius_meters": 2000}]
        # Ping ~500m away (would fail with 200m default, but passes with 2km)
        pings = [
            {"timestamp": "2026-03-01T18:00:00Z", "lat": 34.055, "lon": -118.4},
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["radius_meters"] == 2000


# ===========================================================================
# Reverse geocode cache DB tests
# ===========================================================================


class TestReverseGeocodeCache:
    def test_cache_miss_returns_none(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result is None

    def test_store_and_retrieve(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data = {
                "display_name": "123 Main St, Los Angeles, CA",
                "neighborhood": "Downtown",
                "suburb": "Central LA",
                "road": "Main St",
                "city": "Los Angeles",
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data)
            conn.commit()

            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result is not None
            assert result["display_name"] == "123 Main St, Los Angeles, CA"
            assert result["neighborhood"] == "Downtown"
            assert result["suburb"] == "Central LA"
            assert result["road"] == "Main St"
            assert result["city"] == "Los Angeles"

    def test_rounding_hits_same_entry(self, tmp_path):
        """Nearby coords (within ~11m) should hit the same cache entry."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data = {
                "display_name": "Test Place",
                "neighborhood": None,
                "suburb": None,
                "road": "Test Rd",
                "city": "Test City",
            }
            db.cache_reverse_geocode(conn, 34.05001, -118.25002, data)
            conn.commit()

            # Slightly different coords that round to the same 4-decimal value
            result = db.get_reverse_geocode(conn, 34.05004, -118.25001)
            assert result is not None
            assert result["display_name"] == "Test Place"

    def test_upsert_overwrites(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            data1 = {
                "display_name": "Old Name",
                "neighborhood": None,
                "suburb": None,
                "road": None,
                "city": None,
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data1)
            conn.commit()

            data2 = {
                "display_name": "New Name",
                "neighborhood": "New Hood",
                "suburb": None,
                "road": None,
                "city": None,
            }
            db.cache_reverse_geocode(conn, 34.05, -118.25, data2)
            conn.commit()

            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert result["display_name"] == "New Name"
            assert result["neighborhood"] == "New Hood"


# ===========================================================================
# Reverse geocode function tests (geo.py)
# ===========================================================================


class TestReverseGeocode:
    def test_cache_hit(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, {
                "display_name": "Cached Place",
                "neighborhood": "Hood",
                "suburb": "Sub",
                "road": "Road",
                "city": "City",
            })
            conn.commit()

            result = reverse_geocode(34.05, -118.25, conn)
            assert result["source"] == "cache"
            assert result["display_name"] == "Cached Place"

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            mock_result = MagicMock()
            mock_result.address = "456 Oak Ave, Pasadena, CA"
            mock_result.raw = {
                "address": {
                    "road": "Oak Ave",
                    "neighbourhood": "Old Town",
                    "suburb": "South Pasadena",
                    "city": "Pasadena",
                }
            }

            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.return_value = mock_result
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(34.15, -118.14, conn)
                assert result["source"] == "nominatim"
                assert result["display_name"] == "456 Oak Ave, Pasadena, CA"
                assert result["road"] == "Oak Ave"
                assert result["neighborhood"] == "Old Town"

                # Should be cached now
                cached = db.get_reverse_geocode(conn, 34.15, -118.14)
                assert cached is not None
                assert cached["display_name"] == "456 Oak Ave, Pasadena, CA"

    @_needs_geopy
    def test_nominatim_returns_none(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.return_value = None
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(0.0, 0.0, conn)
                assert result["source"] == "error"
                assert "error" in result

    @_needs_geopy
    def test_nominatim_exception(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            with patch("geopy.geocoders.Nominatim") as mock_nom_cls:
                mock_geolocator = MagicMock()
                mock_geolocator.reverse.side_effect = Exception("timeout")
                mock_nom_cls.return_value = mock_geolocator

                result = reverse_geocode(34.05, -118.25, conn)
                assert result["source"] == "error"
                assert "timeout" in result["error"]


# ===========================================================================
# Cluster pings tests (geo.py)
# ===========================================================================


class TestFilterTransitClusters:
    """Direct unit tests for filter_transit_clusters() spatial absorption."""

    def _make_cluster(self, lat, lon, first_ts, last_ts, ping_count,
                      place_name=None, place_id=None):
        return {
            "lat": lat, "lon": lon,
            "first_ts": first_ts, "last_ts": last_ts,
            "ping_count": ping_count,
            "place_name": place_name, "place_id": place_id,
        }

    def test_absorbs_nearby_fragment_into_previous_stop(self):
        """Small cluster within merge radius of previous stop gets absorbed."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Big stop — survives filtering on its own
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z",
                               ping_count=20),
            # Small fragment — same location, indoor GPS gap
            self._make_cluster(34.0837, -118.3100,
                               "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 1
        # Fragment absorbed: ping count summed, last_ts extended
        assert stops[0]["ping_count"] == 22
        assert stops[0]["last_ts"] == "2026-04-08T19:28:00Z"
        assert transit == 0

    def test_discards_distant_fragment(self):
        """Small cluster far from previous stop is still discarded as transit."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Stop at location A
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z",
                               ping_count=20),
            # Small fragment at a different location (~1km away)
            self._make_cluster(34.0920, -118.3101,
                               "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 1
        assert stops[0]["ping_count"] == 20  # not absorbed
        assert transit == 2

    def test_no_previous_stop_to_absorb_into(self):
        """First cluster is small with no preceding stop — discarded normally."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Small cluster, nothing to absorb into
            self._make_cluster(34.0836, -118.3101,
                               "2026-04-08T19:07:00Z", "2026-04-08T19:08:00Z",
                               ping_count=2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 0
        assert transit == 2


class TestMergeConsecutiveStops:
    """Direct unit tests for merge_consecutive_stops() spatial proximity merge."""

    def _make_stop(self, location, lat, lon, first_ts, last_ts, ping_count,
                   location_source="nominatim", transit_before=0):
        return {
            "location": location,
            "location_source": location_source,
            "lat": lat, "lon": lon,
            "first_ts": first_ts, "last_ts": last_ts,
            "first_ts_local": first_ts[-9:-4] if len(first_ts) > 9 else first_ts,
            "last_ts_local": last_ts[-9:-4] if len(last_ts) > 9 else last_ts,
            "ping_count": ping_count,
            "_transit_pings_before": transit_before,
        }

    def test_merges_nearby_unnamed_stops_with_different_names(self):
        """Two consecutive stops ~20m apart with different reverse-geocoded names
        should merge (ISSUE-047 bug A)."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("East Live Oak Drive", 34.1086, -118.3099,
                            "2026-04-10T01:58:00Z", "2026-04-10T03:56:00Z", 33),
            self._make_stop("Tryon Road", 34.1087, -118.3100,
                            "2026-04-10T04:09:00Z", "2026-04-10T04:41:00Z", 11),
            self._make_stop("East Live Oak Drive", 34.1086, -118.3099,
                            "2026-04-10T05:05:00Z", "2026-04-10T05:16:00Z", 18),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["ping_count"] == 62
        # Should keep the name from the longest stop
        assert merged[0]["location"] == "East Live Oak Drive"

    def test_does_not_merge_distant_unnamed_stops(self):
        """Two consecutive unnamed stops far apart should not merge."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Elm Street", 34.05, -118.25,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            self._make_stop("Oak Avenue", 34.06, -118.25,
                            "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2

    def test_does_not_proximity_merge_saved_places(self):
        """Two different saved places nearby should not be merged by proximity."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Home", 34.1025, -118.3059,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20,
                            location_source="saved_place"),
            self._make_stop("Neighbor", 34.1026, -118.3060,
                            "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15,
                            location_source="saved_place"),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2

    def test_proximity_merge_keeps_longer_stop_name(self):
        """When merging by proximity, the name from the longer stop is kept."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Short Road", 34.1086, -118.3099,
                            "2026-04-10T10:00:00Z", "2026-04-10T10:10:00Z", 5),
            self._make_stop("Main Boulevard", 34.1087, -118.3100,
                            "2026-04-10T10:15:00Z", "2026-04-10T12:00:00Z", 30),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["location"] == "Main Boulevard"

    def test_proximity_merge_respects_transit_threshold(self):
        """Even if nearby, stops separated by significant transit should not merge."""
        from istota.geo import merge_consecutive_stops

        stops = [
            self._make_stop("Road A", 34.1086, -118.3099,
                            "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            self._make_stop("Road B", 34.1087, -118.3100,
                            "2026-04-10T12:00:00Z", "2026-04-10T13:00:00Z", 15,
                            transit_before=5),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 2


class TestClusterPings:
    def test_empty_input(self):
        from istota.geo import cluster_pings

        assert cluster_pings([]) == []

    def test_single_ping(self):
        from istota.geo import cluster_pings

        pings = [{"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z"}]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["ping_count"] == 1
        assert result[0]["lat"] == 34.05
        assert result[0]["first_ts"] == "2026-03-08T10:00:00Z"
        assert result[0]["last_ts"] == "2026-03-08T10:00:00Z"

    def test_two_close_pings_one_cluster(self):
        from istota.geo import cluster_pings

        # Two pings ~10m apart — well within 200m default radius
        pings = [
            {"lat": 34.05000, "lon": -118.25000, "timestamp": "2026-03-08T10:00:00Z"},
            {"lat": 34.05005, "lon": -118.25005, "timestamp": "2026-03-08T10:05:00Z"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["ping_count"] == 2

    def test_two_distant_pings_two_clusters(self):
        from istota.geo import cluster_pings

        # Two pings ~5km apart
        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z"},
            {"lat": 34.10, "lon": -118.25, "timestamp": "2026-03-08T11:00:00Z"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 2
        assert result[0]["ping_count"] == 1
        assert result[1]["ping_count"] == 1

    def test_cluster_carries_place_info(self):
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-03-08T10:00:00Z",
             "place_id": 42, "place_name": "home"},
            {"lat": 34.05001, "lon": -118.25001, "timestamp": "2026-03-08T10:05:00Z",
             "place_id": 42, "place_name": "home"},
        ]
        result = cluster_pings(pings)
        assert len(result) == 1
        assert result[0]["place_id"] == 42
        assert result[0]["place_name"] == "home"

    def test_centroid_drift_splits_route(self):
        """Many pings drifting slowly along a road should NOT merge into one cluster.

        Simulates riding ~500m along a street with 120 pings (~4m each).
        Each ping is close to the drifting centroid but far from the origin.
        The origin anchor should force a split.
        """
        from istota.geo import cluster_pings

        # 120 pings drifting ~4m each north (~480m total), like riding through
        # an intersection over ~10 minutes. Each ping only ~4m from centroid
        # so old code absorbs them all into one cluster.
        pings = [
            {"lat": 34.0500 + i * 0.000036, "lon": -118.25,
             "timestamp": f"2026-04-03T17:{i // 12:02d}:{(i % 12) * 5:02d}Z"}
            for i in range(120)
        ]
        result = cluster_pings(pings, radius_m=250)
        # Origin anchor should split this into multiple clusters
        assert len(result) >= 2
        # No single cluster should span the full route
        assert all(c["ping_count"] < 120 for c in result)

    def test_origin_anchor_forces_split(self):
        """A ping within centroid radius but beyond 1.5x origin radius must split.

        Four pings: A at origin, B1+B2 at ~178m (shifting centroid to ~119m),
        then C at ~311m from origin. C is within 200m of the centroid but
        beyond the 1.5*200=300m origin limit.
        """
        from istota.geo import cluster_pings, haversine

        a  = {"lat": 34.0500, "lon": -118.25, "timestamp": "2026-04-03T17:00:00Z"}
        b1 = {"lat": 34.0516, "lon": -118.25, "timestamp": "2026-04-03T17:01:00Z"}
        b2 = {"lat": 34.0516, "lon": -118.25, "timestamp": "2026-04-03T17:02:00Z"}
        c  = {"lat": 34.0528, "lon": -118.25, "timestamp": "2026-04-03T17:03:00Z"}

        # Verify geometry: C is within 200m of centroid(A,B1,B2) but >300m from A
        centroid_lat = (a["lat"] + b1["lat"] + b2["lat"]) / 3
        assert haversine(centroid_lat, -118.25, c["lat"], -118.25) < 200
        assert haversine(a["lat"], -118.25, c["lat"], -118.25) > 300

        result = cluster_pings([a, b1, b2, c], radius_m=200)
        assert len(result) == 2
        assert result[0]["ping_count"] == 3  # A + B1 + B2
        assert result[1]["ping_count"] == 1  # C split off by origin anchor

    def test_time_gap_splits_cluster(self):
        """Pings at the same location but >5 min apart should split."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:00:00Z"},
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:01:00Z"},
            # 10-minute gap
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:11:00Z"},
            {"lat": 34.05, "lon": -118.25, "timestamp": "2026-04-03T10:12:00Z"},
        ]
        result = cluster_pings(pings, max_gap_seconds=300)
        assert len(result) == 2
        assert result[0]["ping_count"] == 2
        assert result[1]["ping_count"] == 2

    def test_stationary_pings_cluster_normally(self):
        """Pings at the same spot with no time gaps stay in one cluster."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": f"2026-04-03T10:{i:02d}:00Z"}
            for i in range(20)
        ]
        result = cluster_pings(pings, radius_m=200)
        assert len(result) == 1
        assert result[0]["ping_count"] == 20


# ===========================================================================
# reverse-geocode CLI command tests
# ===========================================================================


class TestCmdReverseGeocode:
    def test_returns_json(self, tmp_path):
        from istota.skills.location import cmd_reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, {
                "display_name": "Test Place",
                "neighborhood": "Hood",
                "suburb": "Sub",
                "road": "Road",
                "city": "City",
            })
            conn.commit()

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        args = MagicMock()
        args.lat = 34.05
        args.lon = -118.25

        with patch.dict("os.environ", env, clear=False):
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_reverse_geocode(args)
            finally:
                sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["source"] == "cache"
        assert result["display_name"] == "Test Place"

    @_needs_geopy
    def test_nominatim_fallback(self, tmp_path):
        from istota.skills.location import cmd_reverse_geocode

        db_path = _init_db(tmp_path)

        env = {"ISTOTA_DB_PATH": str(db_path), "ISTOTA_USER_ID": "alice"}
        args = MagicMock()
        args.lat = 34.15
        args.lon = -118.14

        mock_result = MagicMock()
        mock_result.address = "789 Pine St"
        mock_result.raw = {"address": {"road": "Pine St", "city": "Glendale"}}

        with patch.dict("os.environ", env, clear=False), \
             patch("geopy.geocoders.Nominatim") as mock_nom_cls:
            mock_geolocator = MagicMock()
            mock_geolocator.reverse.return_value = mock_result
            mock_nom_cls.return_value = mock_geolocator

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_reverse_geocode(args)
            finally:
                sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["source"] == "nominatim"
        assert result["road"] == "Pine St"


# ===========================================================================
# day-summary CLI command tests
# ===========================================================================


@_needs_geopy
class TestCmdDaySummary:
    def _run_day_summary(self, tmp_path, pings=None, places=None,
                         date="2026-03-08", tz="America/Los_Angeles",
                         nominatim_results=None):
        """Helper to run cmd_day_summary with test DB and optional mocks."""
        from istota.skills.location import cmd_day_summary

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            for p in (places or []):
                db.insert_place(conn, "alice", p["name"], p["lat"], p["lon"],
                                p.get("radius_meters", 100), p.get("category", "other"))
            for ping in (pings or []):
                place_id = ping.get("place_id")
                db.insert_location_ping(
                    conn, "alice", ping["timestamp"], ping["lat"], ping["lon"],
                    accuracy=ping.get("accuracy", 5.0),
                    place_id=place_id,
                )
            conn.commit()

        env = {
            "ISTOTA_DB_PATH": str(db_path),
            "ISTOTA_USER_ID": "alice",
            "TZ": tz,
        }
        args = MagicMock()
        args.date = date
        args.tz = tz

        mock_nom = MagicMock()
        if nominatim_results:
            mock_nom.reverse.side_effect = nominatim_results
        else:
            mock_nom.reverse.return_value = None

        with patch.dict("os.environ", env, clear=False), \
             patch("geopy.geocoders.Nominatim", return_value=mock_nom):
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_day_summary(args)
            finally:
                sys.stdout = old_stdout

        return json.loads(captured.getvalue())

    def test_no_pings_empty_stops(self, tmp_path):
        result = self._run_day_summary(tmp_path)
        assert result["date"] == "2026-03-08"
        assert result["stops"] == []
        assert result["ping_count"] == 0

    def test_single_stop_at_saved_place(self, tmp_path):
        """Pings at a saved place should use the place name."""
        # March 8 in PST = UTC 2026-03-08T08:00:00Z to 2026-03-09T08:00:00Z
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        # Insert a place and get its ID — we use place_id=1 since it's the first insert
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T18:00:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "home"
        assert result["stops"][0]["location_source"] == "saved_place"
        assert result["stops"][0]["ping_count"] == 3

    def test_transit_filtered(self, tmp_path):
        """Clusters with <=2 pings and no place match should be excluded as transit."""
        pings = [
            # 3 pings at one spot (kept)
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.0501, "lon": -118.2501},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.0502, "lon": -118.2502},
            # 1 ping far away (filtered as transit)
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.15, "lon": -118.35},
        ]
        result = self._run_day_summary(tmp_path, pings=pings)
        assert len(result["stops"]) == 1
        assert result["transit_pings"] == 1

    def test_proximity_place_match(self, tmp_path):
        """Cluster centroid near a saved place (within radius) uses place name."""
        places = [{"name": "cafe", "lat": 34.05, "lon": -118.25, "radius_meters": 50}]
        # Pings ~30m from saved place — within max(50, 100) = 100m
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05025, "lon": -118.25},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.05027, "lon": -118.25001},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.05029, "lon": -118.25002},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "cafe"
        assert result["stops"][0]["location_source"] == "saved_place_proximity"

    def test_reverse_geocode_fallback(self, tmp_path):
        """When no place match, reverse geocode should be used."""
        mock_result = MagicMock()
        mock_result.address = "789 Elm St, Burbank, CA"
        mock_result.raw = {
            "address": {
                "road": "Elm St",
                "suburb": "Magnolia Park",
                "city": "Burbank",
            }
        }

        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings,
            nominatim_results=[mock_result],
        )
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "Magnolia Park"
        assert result["stops"][0]["suburb"] == "Magnolia Park"

    def test_consecutive_same_location_merged(self, tmp_path):
        """Two consecutive clusters at the same saved place should merge."""
        places = [{"name": "office", "lat": 34.05, "lon": -118.25, "radius_meters": 200}]
        pings = [
            # Cluster 1 at office
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
            # Brief transit ping (filtered out)
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.15, "lon": -118.35},
            # Cluster 2 at office again
            {"timestamp": "2026-03-08T18:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T18:05:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T18:10:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        # Two clusters at "office" with transit filtered → should merge into one
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "office"
        assert result["stops"][0]["ping_count"] == 6

    def test_same_location_not_merged_after_real_trip(self, tmp_path):
        """Home→trip→Home should show two separate Home stops, not one merged."""
        places = [
            {"name": "Home", "lat": 34.1025, "lon": -118.3059, "radius_meters": 100},
            {"name": "Restaurant", "lat": 34.076, "lon": -118.305, "radius_meters": 100},
        ]
        pings = [
            # Home cluster 1
            {"timestamp": "2026-03-09T00:50:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:50:30Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:52:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            # Driving away (many transit pings)
            {"timestamp": "2026-03-09T02:48:00Z", "lat": 34.1029, "lon": -118.3068},
            {"timestamp": "2026-03-09T02:48:10Z", "lat": 34.1017, "lon": -118.3078},
            {"timestamp": "2026-03-09T02:48:20Z", "lat": 34.1017, "lon": -118.3088},
            {"timestamp": "2026-03-09T02:48:30Z", "lat": 34.1006, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:49:00Z", "lat": 34.0981, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:49:30Z", "lat": 34.0960, "lon": -118.3093},
            {"timestamp": "2026-03-09T02:50:00Z", "lat": 34.0937, "lon": -118.3092},
            {"timestamp": "2026-03-09T02:51:00Z", "lat": 34.0870, "lon": -118.3092},
            {"timestamp": "2026-03-09T02:52:00Z", "lat": 34.0806, "lon": -118.3091},
            # Dinner (few pings, short dwell, no saved place nearby)
            {"timestamp": "2026-03-09T02:58:00Z", "lat": 34.070, "lon": -118.300},
            {"timestamp": "2026-03-09T02:59:00Z", "lat": 34.070, "lon": -118.300},
            # Driving back
            {"timestamp": "2026-03-09T03:37:00Z", "lat": 34.080, "lon": -118.309},
            {"timestamp": "2026-03-09T03:38:00Z", "lat": 34.087, "lon": -118.309},
            {"timestamp": "2026-03-09T03:40:00Z", "lat": 34.094, "lon": -118.309},
            {"timestamp": "2026-03-09T03:43:00Z", "lat": 34.097, "lon": -118.309},
            {"timestamp": "2026-03-09T03:45:00Z", "lat": 34.100, "lon": -118.309},
            # Home cluster 2
            {"timestamp": "2026-03-09T03:47:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T03:48:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T03:53:00Z", "lat": 34.1026, "lon": -118.3058, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places,
                                        date="2026-03-08", tz="America/Los_Angeles")
        home_stops = [s for s in result["stops"] if s["location"] == "Home"]
        assert len(home_stops) == 2, (
            f"Expected 2 Home stops (left and returned), got {len(home_stops)}: {result['stops']}"
        )

    def test_same_location_merged_after_phone_sleep(self, tmp_path):
        """Home with phone sleep gap (no transit) should merge into one stop."""
        places = [
            {"name": "Home", "lat": 34.1025, "lon": -118.3059, "radius_meters": 100},
        ]
        pings = [
            # Home cluster 1
            {"timestamp": "2026-03-09T00:50:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T00:52:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            # 2-hour gap (phone sleeping, no pings at all)
            # Home cluster 2
            {"timestamp": "2026-03-09T02:46:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
            {"timestamp": "2026-03-09T02:48:00Z", "lat": 34.1025, "lon": -118.3059, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places,
                                        date="2026-03-08", tz="America/Los_Angeles")
        home_stops = [s for s in result["stops"] if s["location"] == "Home"]
        assert len(home_stops) == 1, (
            f"Expected 1 merged Home stop (phone sleep, no transit), got {len(home_stops)}"
        )

    def test_indoor_gps_gaps_preserve_stop(self, tmp_path):
        """Indoor GPS gaps should not drop a stop from the summary.

        Simulates the ISSUE-043 scenario: phone at a restaurant for ~95 min
        with large gaps between pings due to indoor GPS signal loss.
        """
        lat, lon = 34.0836, -118.3101
        pings = [
            # Cluster 1: strong initial fix (7:07-7:21 PM PST = 03:07-03:21 UTC)
            *[{"timestamp": f"2026-03-09T03:{7+i:02d}:00Z", "lat": lat + i*0.00001,
               "lon": lon, "place_id": None}
              for i in range(15)],
            # 6-minute gap (indoor)
            # Cluster 2: brief fix (7:27 PM)
            {"timestamp": "2026-03-09T03:27:00Z", "lat": lat + 0.00005, "lon": lon, "place_id": None},
            # 40-minute gap (deep indoor)
            # Cluster 3: brief fix (8:07 PM)
            {"timestamp": "2026-03-09T04:07:00Z", "lat": lat - 0.00003, "lon": lon, "place_id": None},
            {"timestamp": "2026-03-09T04:08:00Z", "lat": lat - 0.00002, "lon": lon, "place_id": None},
            # 20-minute gap
            # Cluster 4: leaving (8:28-8:42 PM)
            *[{"timestamp": f"2026-03-09T04:{28+i}:00Z", "lat": lat + i*0.00001,
               "lon": lon, "place_id": None}
              for i in range(5)],
        ]
        result = self._run_day_summary(tmp_path, pings=pings,
                                        date="2026-03-08", tz="America/Los_Angeles")
        # All pings are at the same location — should be one stop
        assert len(result["stops"]) == 1, (
            f"Expected 1 stop (indoor GPS gaps), got {len(result['stops'])}: {result['stops']}"
        )
        # The stop should span the full visit
        assert result["stops"][0]["ping_count"] == len(pings)

    def test_duration_minutes_in_output(self, tmp_path):
        """Each stop should include a pre-computed duration_minutes field (ISSUE-047 bug B)."""
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        # 2 hours at home (16:00-18:00 UTC on March 8 = within PST day)
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T17:00:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T18:00:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = self._run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        stop = result["stops"][0]
        assert "duration_minutes" in stop
        assert stop["duration_minutes"] == 120

    def test_duration_minutes_for_nominatim_stop(self, tmp_path):
        """duration_minutes should work for reverse-geocoded stops too."""
        mock_result = MagicMock()
        mock_result.address = "Test Place"
        mock_result.raw = {"address": {"suburb": "TestVille"}}

        # 30-minute stop with pings close enough to avoid cluster splitting
        # (max_gap_seconds=300, so keep gaps under 5 min)
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            {"timestamp": "2026-03-08T16:04:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:08:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:12:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:16:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:20:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:24:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:28:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:30:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings,
            nominatim_results=[mock_result],
        )
        assert len(result["stops"]) == 1
        assert result["stops"][0]["duration_minutes"] == 30

    def test_nearby_stops_with_different_geocoded_names_merge(self, tmp_path):
        """ISSUE-047 scenario: GPS drift causes different road names for same location.

        Three clusters at nearly identical coordinates get different reverse-geocoded
        names. They should merge into a single stop via proximity check.
        """
        # Three nominatim results returning different road names
        results = []
        for road in ["East Live Oak Drive", "Tryon Road", "East Live Oak Drive"]:
            r = MagicMock()
            r.address = f"{road}, Los Feliz, CA"
            r.raw = {"address": {"road": road, "suburb": "Los Feliz"}}
            results.append(r)

        # Three clusters ~110m apart (within 150m merge radius), separated by
        # time gaps that cause cluster splitting. Each cluster is big enough
        # (6+ min dwell, 3+ pings) to independently survive transit filtering,
        # so they reach merge_consecutive_stops as separate stops.
        # Coords differ enough that geocode cache gives different results.
        pings = [
            # Cluster 1: "East Live Oak Drive" — 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{i*2:02d}:00Z",
               "lat": 34.1086, "lon": -118.3099}
              for i in range(5)],
            # > 5min gap → new cluster
            # Cluster 2: "Tryon Road" — ~110m from cluster 1, 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{20+i*2:02d}:00Z",
               "lat": 34.1096, "lon": -118.3099}
              for i in range(5)],
            # > 5min gap → new cluster
            # Cluster 3: "East Live Oak Drive" again, 5 pings over 10 min
            *[{"timestamp": f"2026-03-09T01:{40+i*2:02d}:00Z",
               "lat": 34.1086, "lon": -118.3099}
              for i in range(5)],
        ]
        result = self._run_day_summary(
            tmp_path, pings=pings, date="2026-03-08", tz="America/Los_Angeles",
            nominatim_results=results,
        )
        # All three clusters should merge into one stop
        assert len(result["stops"]) == 1, (
            f"Expected 1 merged stop, got {len(result['stops'])}: "
            f"{[s['location'] for s in result['stops']]}"
        )
        assert result["stops"][0]["ping_count"] == 15


# ===========================================================================
# Accuracy gate + dwell-based exit + reconciliation
# ===========================================================================


@_needs_fastapi
class TestAccuracyGate:
    """Low-accuracy pings must not be matched to places or move the state machine."""

    def _feature(self, lat, lon, ts, accuracy):
        return {
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"timestamp": ts, "horizontal_accuracy": accuracy},
        }

    def test_low_accuracy_ping_not_assigned_to_place(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741, radius_meters=200)
            places = db.get_places(conn, "alice")

            # Set module config with default 100m threshold
            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = self._feature(35.629, 139.741, "2026-04-21T08:19:35Z", accuracy=1336)
            wr._process_feature(conn, "alice", feat, places)
            conn.commit()

            pings = db.get_pings(conn, "alice")
            assert len(pings) == 1
            assert pings[0].place_id is None, (
                "1336m accuracy ping should not have been assigned to the place"
            )
            # State machine must not have opened a visit from the bad ping
            assert db.get_open_visit(conn, "alice") is None

    def test_good_accuracy_ping_is_assigned(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741, radius_meters=200)
            places = db.get_places(conn, "alice")

            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = self._feature(35.629, 139.741, "2026-04-21T08:20:00Z", accuracy=15)
            wr._process_feature(conn, "alice", feat, places)
            conn.commit()

            pings = db.get_pings(conn, "alice")
            assert pings[0].place_id == pid

    def test_null_accuracy_passes(self, tmp_path, monkeypatch):
        """Missing accuracy shouldn't cause us to drop the ping silently."""
        from istota import webhook_receiver as wr
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741, radius_meters=200)
            places = db.get_places(conn, "alice")

            cfg = MagicMock()
            cfg.location.accuracy_threshold_m = 100.0
            cfg.location.visit_exit_minutes = 5.0
            monkeypatch.setattr(wr, "_config", cfg)

            feat = {
                "geometry": {"type": "Point", "coordinates": [139.741, 35.629]},
                "properties": {"timestamp": "2026-04-21T08:20:00Z"},
            }
            wr._process_feature(conn, "alice", feat, places)
            conn.commit()

            pings = db.get_pings(conn, "alice")
            assert pings[0].place_id == pid


@_needs_fastapi
class TestDwellBasedExit:
    """Brief GPS flicker out of place radius must not close an open visit."""

    def _process(self, conn, user_id, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = db.insert_location_ping(
            conn, user_id, timestamp, 0.0, 0.0, accuracy=10.0,
            place_id=place_id,
        )
        _update_state_machine(conn, user_id, ping_id, place_id, place, timestamp)
        return ping_id

    def test_flicker_does_not_close_visit(self, tmp_path, monkeypatch):
        """Pings alternating in/out of radius for < dwell threshold keep visit open."""
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            place = db.get_place_by_name(conn, "alice", "home")

            # Open visit with two consecutive in-place pings
            self._process(conn, "alice", pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:00:30Z")

            # Flicker: out, in, out, in — each gap under 2 min
            self._process(conn, "alice", None, None, "2026-04-21T10:01:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:02:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:03:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:04:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:05:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:06:00Z")

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1, "Flicker should not create extra visits"
            assert visits[0].exited_at is None, "Visit should still be open"

    def test_continuous_away_closes_after_threshold(self, tmp_path, monkeypatch):
        """Continuous out-of-place for exit_minutes closes the visit at exit start."""
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            place = db.get_place_by_name(conn, "alice", "home")

            self._process(conn, "alice", pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:05:00Z")

            # Exit: 4 pings spaced across 6 minutes
            self._process(conn, "alice", None, None, "2026-04-21T10:10:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:12:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:14:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:16:00Z")

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].exited_at == "2026-04-21T10:10:00Z", (
                "Exited_at should be the first away ping, not the last"
            )
            # Duration: 10:00 to 10:10 = 600 sec
            assert visits[0].duration_sec == 600

    def test_away_then_return_extends_visit(self, tmp_path, monkeypatch):
        """Briefly away (< threshold), then back — keeps single visit with fuller duration."""
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            place = db.get_place_by_name(conn, "alice", "home")

            self._process(conn, "alice", pid, place, "2026-04-21T10:00:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:05:00Z")
            # 2 min away, then back
            self._process(conn, "alice", None, None, "2026-04-21T10:06:00Z")
            self._process(conn, "alice", None, None, "2026-04-21T10:07:30Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:08:00Z")
            self._process(conn, "alice", pid, place, "2026-04-21T10:20:00Z")

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].exited_at is None, "Visit should still be open"

            # State's exit timer should have been cleared
            state = db.get_location_state(conn, "alice")
            assert state.exit_started_at is None

    def test_direct_place_to_place_closes_old_opens_new(self, tmp_path, monkeypatch):
        """Moving from one named place straight to another closes old + opens new."""
        from istota import webhook_receiver as wr
        cfg = MagicMock()
        cfg.location.visit_exit_minutes = 5.0
        cfg.location.accuracy_threshold_m = 100.0
        monkeypatch.setattr(wr, "_config", cfg)

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_h = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_g = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            home = db.get_place_by_name(conn, "alice", "home")
            gym = db.get_place_by_name(conn, "alice", "gym")

            self._process(conn, "alice", pid_h, home, "2026-04-21T10:00:00Z")
            self._process(conn, "alice", pid_h, home, "2026-04-21T10:05:00Z")
            # Two consecutive gym pings (hysteresis open); dwell threshold not met
            self._process(conn, "alice", pid_g, gym, "2026-04-21T10:06:00Z")
            self._process(conn, "alice", pid_g, gym, "2026-04-21T10:07:00Z")

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 2
            home_visit = [v for v in visits if v.place_name == "home"][0]
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert home_visit.exited_at is not None
            assert gym_visit.exited_at is None


class TestReconcileVisits:
    def _ping(self, conn, user_id, ts, place_id):
        db.insert_location_ping(
            conn, user_id, ts, 0.0, 0.0, accuracy=10.0, place_id=place_id,
        )

    def test_reconciles_fragmented_visit_into_one(self, tmp_path):
        """The Shinagawa case: flicker split a single stay into many short segments."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # 15 pings mostly at place, a handful briefly outside
            at_place = [f"2026-04-21T10:{m:02d}:00Z" for m in range(0, 30, 2)]
            for ts in at_place:
                self._ping(conn, "alice", ts, pid)
            # sprinkle a few unassigned pings in between — gaps < grace
            for ts in ("2026-04-21T10:05:30Z", "2026-04-21T10:13:30Z", "2026-04-21T10:19:30Z"):
                self._ping(conn, "alice", ts, None)
            conn.commit()

            n = db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            assert n == 1
            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"
            assert visits[0].ping_count == 15

    def test_splits_when_gap_exceeds_grace(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # Morning visit
            for m in range(0, 10, 2):
                self._ping(conn, "alice", f"2026-04-21T08:{m:02d}:00Z", pid)
            # 45-min gap (away), no pings at place
            # Evening visit
            for m in range(0, 10, 2):
                self._ping(conn, "alice", f"2026-04-21T09:{m:02d}:00Z", pid)
            conn.commit()

            n = db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            assert n == 2
            visits = sorted(db.get_visits(conn, "alice"), key=lambda v: v.entered_at)
            assert visits[0].entered_at == "2026-04-21T08:00:00Z"
            assert visits[1].entered_at == "2026-04-21T09:00:00Z"

    def test_filters_walkby(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # Only 2 pings at place — below min_pings=3
            self._ping(conn, "alice", "2026-04-21T10:00:00Z", pid)
            self._ping(conn, "alice", "2026-04-21T10:01:00Z", pid)
            conn.commit()

            n = db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            assert n == 0
            assert db.get_visits(conn, "alice") == []

    def test_splits_on_different_place(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_a = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_b = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            for m in range(0, 10, 2):
                self._ping(conn, "alice", f"2026-04-21T10:{m:02d}:00Z", pid_a)
            for m in range(12, 22, 2):
                self._ping(conn, "alice", f"2026-04-21T10:{m:02d}:00Z", pid_b)
            conn.commit()

            n = db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            assert n == 2
            visits = sorted(db.get_visits(conn, "alice"), key=lambda v: v.entered_at)
            assert visits[0].place_name == "home"
            assert visits[1].place_name == "gym"

    def test_preserves_open_visit_outside_window(self, tmp_path):
        """An open visit started before `since` must be left alone."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # Open visit entered before reconcile window
            vid = db.insert_visit(conn, "alice", pid, "home", "2026-04-20T23:00:00Z")
            for m in range(0, 10, 2):
                self._ping(conn, "alice", f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            visits = db.get_visits(conn, "alice")
            # The open visit must still exist and be open
            open_ones = [v for v in visits if v.exited_at is None]
            assert len(open_ones) == 1
            assert open_ones[0].id == vid

    def test_accuracy_filter_drops_bad_pings(self, tmp_path):
        """Historical pings with accuracy > threshold are treated as unassigned."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # One early bad-accuracy ping pinned to the place (like the 1336m Shinagawa case)
            db.insert_location_ping(
                conn, "alice", "2026-04-21T08:00:00Z", 35.629, 139.741,
                accuracy=1200.0, place_id=pid,
            )
            # Real visit starts later with good pings
            for m in range(30, 50, 2):
                db.insert_location_ping(
                    conn, "alice", f"2026-04-21T08:{m:02d}:00Z", 35.629, 139.741,
                    accuracy=10.0, place_id=pid,
                )
            conn.commit()

            # Without filter: the bad ping would anchor a visit starting at 08:00
            db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
                accuracy_threshold_m=100.0,
            )
            conn.commit()

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T08:30:00Z", (
                "Bad-accuracy ping should not have anchored the visit's entered_at"
            )
            assert visits[0].exited_at == "2026-04-21T08:48:00Z"

    def test_replaces_stale_closed_visits_in_window(self, tmp_path):
        """Existing closed visits in the window are dropped before re-derivation."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid = db.insert_place(conn, "alice", "home", 35.629, 139.741)
            # Seed with an incorrect, short closed visit
            stale_id = db.insert_visit(conn, "alice", pid, "home", "2026-04-21T10:05:00Z")
            db.close_visit(conn, stale_id, "2026-04-21T10:07:00Z")
            # Pings showing the true longer stay
            for m in range(0, 30, 2):
                self._ping(conn, "alice", f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            db.reconcile_visits(
                conn, "alice",
                since="2026-04-21T00:00:00Z", until="2026-04-22T00:00:00Z",
                grace_minutes=10.0, min_pings=3, min_dwell_sec=60,
            )
            conn.commit()

            visits = db.get_visits(conn, "alice")
            assert len(visits) == 1
            assert visits[0].id != stale_id  # stale row deleted
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"
