"""Tests for location tracking: loader, DB functions, haversine, state machine, CLI."""

import io
import json
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta
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
from istota.geo import haversine
from istota.location import db as location_db

if _has_fastapi:
    from istota.webhook_receiver import resolve_place


def _init_db(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _init_loc_db(tmp_path, name: str = "location.db"):
    """Initialise a per-user location.db at ``tmp_path / name`` and return
    the path. Used by tests that exercise post-Stage-2 APIs whose ``db_path``
    arg now refers to the per-user file rather than framework istota.db."""
    db_path = tmp_path / name
    location_db.init_db(db_path)
    return db_path


def _add_place(db_path, name, lat=34.0, lon=-118.0, **kw):
    with location_db.connect(db_path) as conn:
        pid = location_db.add_place(conn, name, lat, lon, **kw)
        conn.commit()
    return pid


def _seed_cluster(conn, lat, lon, count=15):
    for i in range(count):
        ts = f"2026-01-10T09:{i:02d}:00Z"
        # Tiny jitter so they share a rounded grid cell
        location_db.insert_ping(
            conn, ts, lat + (i % 3) * 0.00002, lon,
            accuracy=5.0, activity_type="stationary",
        )


def _home_and_gym(conn):
    pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
    pid_gym = location_db.add_place(conn, "gym", 34.1, -118.1)
    home = location_db.get_place_by_name(conn, "home")
    gym = location_db.get_place_by_name(conn, "gym")
    return (pid_home, home), (pid_gym, gym)


def _location_config(monkeypatch):
    from istota import webhook_receiver as wr
    cfg = MagicMock()
    cfg.location.accuracy_threshold_m = 100.0
    cfg.location.visit_exit_minutes = 5.0
    monkeypatch.setattr(wr, "_config", cfg)


def _skill():
    from istota.skills import location
    return location


def _capture(fn, args, *, exit_code=None):
    """Call a location CLI command and return its parsed JSON stdout.

    With ``exit_code`` the command must exit with that status.
    """
    captured = io.StringIO()
    with patch.object(sys, "stdout", captured):
        if exit_code is None:
            fn(args)
        else:
            with pytest.raises(SystemExit) as exc:
                fn(args)
            assert exc.value.code == exit_code
    return json.loads(captured.getvalue())


def _run_cmd(fn, **args):
    """Call a location CLI command and return its parsed JSON stdout.

    Args are a ``SimpleNamespace``, not a ``MagicMock``: a Mock answers every
    attribute with a truthy Mock, so a command reading an option the test never
    set silently takes a fallback branch instead of failing.
    """
    return _capture(fn, SimpleNamespace(**args))


def _loc_env(db_path, framework_db=None):
    return patch.dict("os.environ", {
        "LOCATION_DB_PATH": str(db_path),
        "ISTOTA_DB_PATH": str(framework_db or db_path),
    })


def _run_cli(fn, db_path, *, exit_code=None, **attrs):
    """Run a command against ``db_path`` with ``MagicMock`` args carrying ``attrs``."""
    args = MagicMock()
    for key, value in attrs.items():
        setattr(args, key, value)
    with _loc_env(db_path):
        return _capture(fn, args, exit_code=exit_code)


def _patch_nominatim(method, *, returns=None, side_effect=None):
    geolocator = MagicMock()
    getattr(geolocator, method).return_value = returns
    getattr(geolocator, method).side_effect = side_effect
    return patch("geopy.geocoders.Nominatim", return_value=geolocator)


def _nominatim_hit(address, **fields):
    result = MagicMock()
    result.address = address
    result.raw = {"address": fields}
    return result


def _geo_row(display_name, **fields):
    row = {"display_name": display_name, "neighborhood": None, "suburb": None,
           "road": None, "city": None}
    row.update(fields)
    return row


# ===========================================================================
# DB function tests
# ===========================================================================


# Per-user equivalents of the framework db.* helper tests live in
# tests/test_location_module.py — TestLocationPingDB / TestPlaceDB /
# TestDismissedClusterDB / TestLocationStateDB were removed in Stage 4
# along with the framework helpers themselves.


@_needs_fastapi
class TestPlaceNotesAPI:
    @pytest.mark.parametrize("fields, stored", [
        ({"radius_meters": 100, "category": "work", "notes": "side entrance, 4th floor"},
         "side entrance, 4th floor"),
        ({"notes": "   "}, None),
    ], ids=["persists_notes", "empty_notes_stored_as_null"])
    def test_create(self, tmp_path, fields, stored):
        from istota.web_app import _location_create_place, _location_query_places

        db_path = _init_loc_db(tmp_path)
        _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0, **fields,
        })

        result = _location_query_places(str(db_path))
        assert result["places"][0]["notes"] == stored

    @pytest.mark.parametrize("notes, stored", [
        ("new", "new"),
        ("", None),
    ], ids=["changes_notes", "empty_notes_clears_field"])
    def test_update(self, tmp_path, notes, stored):
        from istota.web_app import _location_create_place, _location_update_place

        db_path = _init_loc_db(tmp_path)
        created = _location_create_place(str(db_path), {
            "name": "office", "lat": 34.0, "lon": -118.0, "notes": "old",
        })

        result = _location_update_place(str(db_path), created["id"], {"notes": notes})
        assert result["notes"] == stored


@_needs_fastapi
class TestDiscoverPlacesFiltersDismissed:
    @pytest.mark.parametrize("dismissed, expected", [
        (None, 1),
        ((34.0, -118.0), 0),
        ((40.0, -73.0), 1),  # a different city
    ], ids=["unknown_cluster_appears", "dismissed_cluster_is_filtered",
            "distant_dismissal_does_not_filter"])
    def test_dismissal(self, tmp_path, dismissed, expected):
        from istota.web_app import _location_discover_places

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _seed_cluster(conn, 34.0, -118.0)
            if dismissed:
                location_db.dismiss_cluster(conn, *dismissed, 200)
            conn.commit()

        result = _location_discover_places(str(db_path), min_pings=10)
        assert len(result["clusters"]) == expected
        if expected:
            assert "radius_meters" in result["clusters"][0]

    def test_dismissed_zone_only_affects_owner(self, tmp_path):
        from istota.web_app import _location_discover_places

        # Per-user split: alice and bob now live in separate location.db
        # files. Seed both, dismiss in alice's only, assert isolation.
        alice_db = _init_loc_db(tmp_path, name="alice.db")
        bob_db = _init_loc_db(tmp_path, name="bob.db")
        with location_db.connect(alice_db) as conn:
            _seed_cluster(conn, 34.0, -118.0)
            location_db.dismiss_cluster(conn, 34.0, -118.0, 200)
            conn.commit()
        with location_db.connect(bob_db) as conn:
            _seed_cluster(conn, 34.0, -118.0)
            conn.commit()

        assert _location_discover_places(str(alice_db), min_pings=10)["clusters"] == []
        assert len(_location_discover_places(str(bob_db), min_pings=10)["clusters"]) == 1


class TestVisitDB:
    def test_insert_and_close(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            vid = location_db.open_visit(conn, pid, "home", "2026-02-20T08:00:00")
            conn.commit()

            visit = location_db.get_open_visit(conn)
            assert visit is not None
            assert visit.place_name == "home"
            assert visit.exited_at is None

            location_db.close_visit(conn, vid, "2026-02-20T10:00:00")
            conn.commit()

            assert location_db.get_open_visit(conn) is None

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].exited_at == "2026-02-20T10:00:00"
            assert visits[0].duration_sec > 0

    def test_increment_ping_count(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            vid = location_db.open_visit(conn, None, "unknown", "2026-02-20T08:00:00")
            location_db.increment_visit_ping_count(conn, vid)
            location_db.increment_visit_ping_count(conn, vid)
            conn.commit()

            visit = location_db.get_open_visit(conn)
            assert visit.ping_count == 3  # 1 initial + 2 increments


def _add_pings_at(conn, place_id, times, day="2026-01-10"):
    """Insert stationary pings at a place, one per ``HH:MM`` in ``times``."""
    for hhmm in times:
        location_db.insert_ping(
            conn, f"{day}T{hhmm}:00Z", 34.0, -118.0,
            accuracy=5.0, activity_type="stationary",
            place_id=place_id,
        )


@_needs_fastapi
class TestPlaceStats:
    @pytest.mark.parametrize("times, expected", [
        ([], {"total_visits": 0, "first_visit": None}),
        (["09:00", "09:05", "09:30", "10:00"],
         {"total_visits": 1, "avg_duration_min": 60, "total_duration_min": 60}),
        # 2-hour gap with no pings elsewhere does not split the visit: 09:00 to 11:20
        (["09:00", "09:05", "09:10", "11:10", "11:15", "11:20"],
         {"total_visits": 1, "total_duration_min": 140}),
        # Fewer than 3 pings is a walk-by and does not count
        (["09:00", "09:05"], {"total_visits": 0}),
    ], ids=["no_pings", "single_visit_from_pings", "gap_without_elsewhere_is_same_visit",
            "walkby_filtered"])
    def test_stats(self, tmp_path, times, expected):
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0, radius_meters=100, category="food",
            )
            _add_pings_at(conn, pid, times)
            conn.commit()

        result = _location_place_stats(str(db_path), pid)
        assert {k: result[k] for k in expected} == expected

    def test_two_visits_split_by_elsewhere(self, tmp_path):
        """Pings at another place during a gap should split into two visits."""
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_cafe = location_db.add_place(
                conn, "cafe", 34.0, -118.0, radius_meters=100, category="food",
            )
            pid_gym = location_db.add_place(
                conn, "gym", 34.01, -118.01, radius_meters=100, category="gym",
            )
            _add_pings_at(conn, pid_cafe, ["09:00", "09:05", "09:10", "09:15", "09:20"])
            _add_pings_at(conn, pid_gym, ["10:00", "10:05"])
            _add_pings_at(conn, pid_cafe, ["11:20", "11:25", "11:30", "11:35"])
            conn.commit()

        result = _location_place_stats(str(db_path), pid_cafe)
        assert result["total_visits"] == 2
        assert result["first_visit"] == "2026-01-10T09:00:00Z"
        assert result["last_visit"] == "2026-01-10T11:20:00Z"

    def test_wrong_user_returns_none(self, tmp_path):
        """Per-user split: a place that doesn't exist in *this* db
        returns None. (The previous "wrong user" semantics is now
        implicit in choosing the wrong db file.)"""
        from istota.web_app import _location_place_stats

        alice_db = _init_loc_db(tmp_path, name="alice.db")
        bob_db = _init_loc_db(tmp_path, name="bob.db")
        pid = _add_place(alice_db, "cafe", radius_meters=100, category="food")

        assert _location_place_stats(str(bob_db), pid) is None

    def test_nonexistent_place_returns_none(self, tmp_path):
        from istota.web_app import _location_place_stats

        db_path = _init_loc_db(tmp_path)
        assert _location_place_stats(str(db_path), 9999) is None


@_needs_fastapi
class TestPlaceUpdateReassignment:
    def test_move_place_reassigns_pings(self, tmp_path):
        """Moving a place center should reassign pings to match the new geofence."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0, radius_meters=50, category="food",
            )
            for ts in ["09:00", "09:05", "09:10", "09:15"]:
                location_db.insert_ping(
                    conn, f"2026-01-10T{ts}:00Z", 34.0001, -118.0,
                    accuracy=5.0, place_id=pid,
                )
            for ts in ["10:00", "10:05", "10:10"]:
                location_db.insert_ping(conn, f"2026-02-10T{ts}:00Z", 34.001, -118.0, accuracy=5.0)
            conn.commit()

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1

        _location_update_place(str(db_path), pid, {"lat": 34.001, "lon": -118.0})

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1
        assert stats["first_visit"] == "2026-02-10T10:00:00Z"

    def test_radius_change_reassigns_pings(self, tmp_path):
        """Expanding radius should pick up nearby unassigned pings."""
        from istota.web_app import _location_update_place, _location_place_stats

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(
                conn, "cafe", 34.0, -118.0, radius_meters=25, category="food",
            )
            for ts in ["09:00", "09:05", "09:10"]:
                location_db.insert_ping(conn, f"2026-01-10T{ts}:00Z", 34.00035, -118.0, accuracy=5.0)
            conn.commit()

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 0

        _location_update_place(str(db_path), pid, {"radius_meters": 100})

        stats = _location_place_stats(str(db_path), pid)
        assert stats["total_visits"] == 1


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
        from istota.location.models import Place
        places = [Place(1, "home", 34.0, -118.0, 200, "home", "", None)]
        result = resolve_place(34.0001, -118.0001, places)
        assert result is not None
        assert result.name == "home"

    def test_outside_radius(self):
        from istota.location.models import Place
        places = [Place(1, "home", 34.0, -118.0, 50, "home", "", None)]
        assert resolve_place(35.0, -119.0, places) is None

    def test_nearest_wins(self):
        from istota.location.models import Place
        places = [
            Place(1, "far", 34.01, -118.0, 5000, "other", "", None),
            Place(2, "near", 34.0001, -118.0001, 5000, "other", "", None),
        ]
        assert resolve_place(34.0, -118.0, places).name == "near"

    def test_empty_places(self):
        assert resolve_place(34.0, -118.0, []) is None


# ===========================================================================
# State machine tests
# ===========================================================================


@_needs_fastapi
class TestStateMachine:
    """Tests for the state machine logic in webhook_receiver."""

    def _process(self, conn, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = location_db.insert_ping(conn, timestamp, 0.0, 0.0)
        _update_state_machine(conn, ping_id, place_id, place, timestamp)
        return ping_id

    def test_first_ping_at_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            place = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid, place, "2026-02-20T10:00:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid
            assert state.current_visit_id is not None
            assert location_db.get_open_visit(conn).place_name == "home"

    def test_first_ping_no_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            self._process(conn, None, None, "2026-02-20T10:00:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id is None
            assert state.current_visit_id is None

    def test_same_place_no_transition(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 34.0, -118.0)
            place = location_db.get_place_by_name(conn, "home")

            for minute in ("00", "05", "10"):
                self._process(conn, pid, place, f"2026-02-20T10:{minute}:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1  # still one visit
            assert visits[0].ping_count == 3

    def test_hysteresis_prevents_single_ping_transition(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            (pid_home, home), (pid_gym, gym) = _home_and_gym(conn)

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")
            self._process(conn, pid_gym, gym, "2026-02-20T10:10:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid_home
            assert state.consecutive_count == 1

    def test_hysteresis_allows_transition_after_threshold(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            (pid_home, home), (pid_gym, gym) = _home_and_gym(conn)

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")
            self._process(conn, pid_gym, gym, "2026-02-20T10:10:00Z")
            self._process(conn, pid_gym, gym, "2026-02-20T10:15:00Z")

            state = location_db.get_location_state(conn)
            assert state.current_place_id == pid_gym

            visits = location_db.get_visits(conn)
            assert len(visits) == 2
            home_visit = [v for v in visits if v.place_name == "home"][0]
            assert home_visit.exited_at is not None
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert gym_visit.exited_at is None

    def test_transition_from_place_to_unknown(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_home = location_db.add_place(conn, "home", 34.0, -118.0)
            home = location_db.get_place_by_name(conn, "home")

            self._process(conn, pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, pid_home, home, "2026-02-20T10:05:00Z")
            self._process(conn, None, None, "2026-02-20T10:10:00Z")
            self._process(conn, None, None, "2026-02-20T10:15:00Z")

            assert location_db.get_location_state(conn).current_place_id is None


# ===========================================================================
# Overland payload parsing tests
# ===========================================================================


@_needs_fastapi
class TestOverlandPayloadParsing:
    """Test that the receiver correctly parses Overland GeoJSON payloads."""

    def _ingest(self, tmp_path, coordinates, **properties):
        """Process one GeoJSON Feature; return (all pings, latest ping)."""
        from istota.webhook_receiver import _process_feature

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coordinates},
            "properties": properties,
        }
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _process_feature(conn, feature, [])
            conn.commit()
            return location_db.get_pings(conn), location_db.get_latest_ping(conn)

    def test_parse_feature_coordinates(self, tmp_path):
        """Verify coordinate extraction from GeoJSON Feature."""
        pings, _ = self._ingest(
            tmp_path, [-122.030581, 37.331800],
            timestamp="2026-02-20T10:30:00-0700", altitude=80, speed=0,
            horizontal_accuracy=5, motion=["stationary"], battery_level=0.92,
            wifi="home-wifi",
        )

        assert len(pings) == 1
        p = pings[0]
        # GeoJSON: coordinates = [lon, lat]
        assert p.lon == -122.030581
        assert p.lat == 37.331800
        assert p.accuracy == 5
        assert p.activity_type == "stationary"
        assert p.battery == 0.92
        assert p.wifi == "home-wifi"

    def test_parse_negative_speed_becomes_none(self, tmp_path):
        _, p = self._ingest(tmp_path, [0, 0], timestamp="2026-01-01T00:00:00Z",
                            speed=-1, course=-1)
        assert p.speed is None
        assert p.course is None

    def test_feature_with_activity_string(self, tmp_path):
        """Overland can send activity as a string instead of motion array."""
        _, p = self._ingest(tmp_path, [0, 0], timestamp="2026-01-01T00:00:00Z",
                            activity="other_navigation")
        assert p.activity_type == "other_navigation"

    def test_empty_coordinates_skipped(self, tmp_path):
        _, latest = self._ingest(tmp_path, [], timestamp="2026-01-01T00:00:00Z")
        assert latest is None


# ===========================================================================
# CLI tests
# ===========================================================================


def _update_args(**overrides):
    args = dict(name=None, id=None, rename=None, category=None, radius=None,
                notes=None, lat=None, lon=None)
    args.update(overrides)
    return args


class TestLocationCLI:
    def test_current_no_data(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        output = _run_cli(_skill().cmd_current, db_path)
        assert output["last_ping"] is None
        assert output["current_visit"] is None

    def test_places_lists_db(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        pid = _add_place(db_path, "home", radius_meters=150, category="home")

        output = _run_cli(_skill().cmd_places, db_path)
        assert len(output) == 1
        assert output[0]["id"] == pid
        assert output[0]["name"] == "home"
        assert output[0]["radius_meters"] == 150

    def test_update_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        _add_place(db_path, "cafe", radius_meters=100, category="restaurant")

        output = _run_cli(_skill().cmd_update, db_path,
                          **_update_args(name="cafe", category="food"))
        assert output["status"] == "ok"
        assert output["place"]["category"] == "food"
        assert output["place"]["name"] == "cafe"

        with location_db.connect(db_path) as conn:
            assert location_db.get_place_by_name(conn, "cafe").category == "food"

    def test_update_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        pid = _add_place(db_path, "cafe", radius_meters=100, category="restaurant")

        output = _run_cli(_skill().cmd_update, db_path, **_update_args(id=pid, category="food"))
        assert output["status"] == "ok"
        assert output["place"]["category"] == "food"

    def test_update_rename(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        _add_place(db_path, "old name", radius_meters=100, category="other")

        output = _run_cli(_skill().cmd_update, db_path,
                          **_update_args(name="old name", rename="new name"))
        assert output["place"]["name"] == "new name"

        with location_db.connect(db_path) as conn:
            assert location_db.get_place_by_name(conn, "new name") is not None
            assert location_db.get_place_by_name(conn, "old name") is None

    @pytest.mark.parametrize("seed, overrides", [
        (False, {"name": "nonexistent", "category": "food"}),
        (True, {"name": "cafe"}),
    ], ids=["not_found", "no_changes"])
    def test_update_fails(self, tmp_path, seed, overrides):
        db_path = _init_loc_db(tmp_path)
        if seed:
            _add_place(db_path, "cafe", radius_meters=100, category="food")

        output = _run_cli(_skill().cmd_update, db_path, exit_code=1,
                          **_update_args(**overrides))
        assert "error" in output

    def test_delete_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        _add_place(db_path, "cafe", radius_meters=100, category="food")

        output = _run_cli(_skill().cmd_delete, db_path, name="cafe", id=None)
        assert output["status"] == "ok"
        assert output["deleted"] == "cafe"

        with location_db.connect(db_path) as conn:
            assert location_db.get_place_by_name(conn, "cafe") is None

    def test_delete_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        pid = _add_place(db_path, "cafe", radius_meters=100, category="food")

        output = _run_cli(_skill().cmd_delete, db_path, name=None, id=pid)
        assert output["status"] == "ok"

        with location_db.connect(db_path) as conn:
            assert location_db.get_places(conn) == []

    def test_delete_not_found(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        output = _run_cli(_skill().cmd_delete, db_path, exit_code=1, name="nonexistent", id=None)
        assert "error" in output

    def test_history_lists_pings(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-02-20T10:00:00Z", 34.0, -118.0,
                accuracy=5.0, activity_type="walking",
            )
            conn.commit()

        output = _run_cli(_skill().cmd_history, db_path, source=None, limit=10, date=None)
        assert len(output) == 1
        assert output[0]["lat"] == 34.0

    def _history_for_mar16(self, db_path, limit):
        return _run_cli(_skill().cmd_history, db_path, source=None, limit=limit,
                        date="2026-03-16", tz="America/Los_Angeles")

    def test_history_date_uses_timezone_aware_boundaries(self, tmp_path):
        """history --date should convert local day boundaries to UTC."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            # 2026-03-16 in Pacific = 2026-03-16T07:00:00Z to 2026-03-17T07:00:00Z (PDT)
            for ts, lat, lon, activity in [
                ("2026-03-16T02:00:00Z", 34.0, -118.0, "stationary"),  # Mar 15 7pm Pacific — outside
                ("2026-03-16T20:00:00Z", 34.1, -118.1, "walking"),     # Mar 16 1pm Pacific — inside
                ("2026-03-17T03:00:00Z", 34.2, -118.2, "walking"),     # Mar 16 8pm Pacific — inside
                ("2026-03-17T10:00:00Z", 34.3, -118.3, "stationary"),  # Mar 17 3am Pacific — outside
            ]:
                location_db.insert_ping(conn, ts, lat, lon, accuracy=5.0, activity_type=activity)
            conn.commit()

        output = self._history_for_mar16(db_path, limit=0)
        assert len(output) == 2
        assert {p["lat"] for p in output} == {34.1, 34.2}

    def test_history_date_returns_all_pings_by_default(self, tmp_path):
        """history --date with no --limit should return all pings, not just 20."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for i in range(30):
                ts = f"2026-03-16T{15 + (i // 6):02d}:{(i % 6) * 10:02d}:00Z"
                location_db.insert_ping(conn, ts, 34.0 + i * 0.001, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        assert len(self._history_for_mar16(db_path, limit=0)) == 30

    def test_history_date_respects_explicit_limit(self, tmp_path):
        """history --date --limit N should cap results."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for i in range(10):
                location_db.insert_ping(conn, f"2026-03-16T{15 + i}:00:00Z", 34.0, -118.0,
                    accuracy=5.0, activity_type="stationary",
                )
            conn.commit()

        assert len(self._history_for_mar16(db_path, limit=5)) == 5


def _seed_altitude(tmp_path):
    db_path = _init_loc_db(tmp_path)
    with location_db.connect(db_path) as conn:
        # A climb: two pings inside 2026-03-16 Pacific, one with no vertical fix.
        location_db.insert_ping(conn, "2026-03-16T20:00:00Z", 34.0, -118.0,
            altitude=335.3, accuracy=5.0, activity_type="driving",
        )
        location_db.insert_ping(conn, "2026-03-16T21:00:00Z", 34.1, -118.1,
            altitude=None, accuracy=5.0, activity_type="driving",
        )
        conn.commit()
    return db_path


def _add_high_ping(db_path):
    with location_db.connect(db_path) as conn:
        location_db.insert_ping(conn, "2026-03-16T22:00:00Z", 34.2, -118.2,
            altitude=1432.6, accuracy=5.0, activity_type="driving",
        )
        conn.commit()


class TestAltitudeSurfacing:
    """ISSUE-218 — altitude is stored on every ping but was dropped by every reader."""

    def test_history_includes_altitude(self, tmp_path):
        """~5% of real pings carry a horizontal fix only; the key must still be present."""
        db_path = _seed_altitude(tmp_path)
        with _loc_env(db_path):
            output = _run_cmd(_skill().cmd_history, limit=10, date=None)

        by_ts = {p["timestamp"]: p for p in output}
        assert by_ts["2026-03-16T20:00:00Z"]["altitude"] == 335.3
        assert by_ts["2026-03-16T21:00:00Z"]["altitude"] is None

    def test_history_date_branch_includes_altitude(self, tmp_path):
        """--date runs a second, separately-written SELECT — it must carry the column too."""
        db_path = _seed_altitude(tmp_path)
        with _loc_env(db_path):
            output = _run_cmd(
                _skill().cmd_history, limit=0, date="2026-03-16", tz="America/Los_Angeles"
            )

        assert len(output) == 2
        assert {p["altitude"] for p in output} == {335.3, None}

    def test_current_includes_altitude(self, tmp_path):
        db_path = _seed_altitude(tmp_path)
        _add_high_ping(db_path)

        with _loc_env(db_path):
            output = _run_cmd(_skill().cmd_current)

        assert output["last_ping"]["altitude"] == 1432.6


@_needs_fastapi
class TestLocationPingsAPIAltitude:
    """The web pings endpoint feeds the map; it dropped altitude the same way."""

    def test_date_range_query_includes_altitude(self, tmp_path):
        from istota.web_app import _location_query_pings

        db_path = _seed_altitude(tmp_path)
        result = _location_query_pings(
            str(db_path), "America/Los_Angeles",
            date="2026-03-16", start=None, end=None, limit=0,
        )

        assert result["count"] == 2
        assert [p["altitude"] for p in result["pings"]] == [335.3, None]

    def test_default_query_includes_altitude(self, tmp_path):
        """The no-date branch is a separate SELECT and needs the column too."""
        from istota.web_app import _location_query_pings

        db_path = _seed_altitude(tmp_path)
        result = _location_query_pings(
            str(db_path), "America/Los_Angeles",
            date=None, start=None, end=None, limit=10,
        )

        assert {p["altitude"] for p in result["pings"]} == {335.3, None}

    def test_current_query_includes_altitude(self, tmp_path):
        """`LocationPing.altitude` is a required field of the shared frontend type,
        so the current-location reader has to send it too — not only the CLI twin."""
        from istota.web_app import _location_query_current

        db_path = _seed_altitude(tmp_path)
        _add_high_ping(db_path)

        assert _location_query_current(str(db_path))["last_ping"]["altitude"] == 1432.6


class TestLocationDiscoverDismissCLI:
    """CLI wrappers for discover, dismiss-cluster, list-dismissed, restore-dismissed, place-stats.

    A not-found verb exits 1 (S10): a call naming a place or a cluster that
    does not exist is a failed call, and a silent exit 0 behind an error
    envelope is what the skill CLI facade exists to stop.
    """

    @pytest.mark.parametrize("count, min_pings, expected", [
        (15, 10, 1),
        (8, 20, 0),
    ], ids=["finds_unassigned_cluster", "respects_min_pings"])
    def test_discover(self, tmp_path, count, min_pings, expected):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _seed_cluster(conn, 34.0, -118.0, count=count)
            conn.commit()

        output = _run_cli(_skill().cmd_discover, db_path, min_pings=min_pings)

        assert len(output["clusters"]) == expected
        if expected:
            assert "lat" in output["clusters"][0]
            assert "radius_meters" in output["clusters"][0]

    def test_dismiss_cluster_inserts_row(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        output = _run_cli(_skill().cmd_dismiss_cluster, db_path, lat=34.0, lon=-118.0, radius=200)

        assert output["status"] == "ok"
        assert output["lat"] == 34.0
        assert output["lon"] == -118.0
        assert output["radius_meters"] == 200
        assert isinstance(output["id"], int)

        with location_db.connect(db_path) as conn:
            assert len(location_db.list_dismissed_clusters(conn)) == 1

    def test_list_dismissed_returns_inserted_rows(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.dismiss_cluster(conn, 34.0, -118.0, 100)
            location_db.dismiss_cluster(conn, 40.0, -73.0, 150)
            conn.commit()

        output = _run_cli(_skill().cmd_list_dismissed, db_path)

        assert len(output["dismissed"]) == 2
        assert sorted(r["radius_meters"] for r in output["dismissed"]) == [100, 150]

    def test_restore_dismissed_deletes_row(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            cid = location_db.dismiss_cluster(conn, 34.0, -118.0, 100)
            conn.commit()

        output = _run_cli(_skill().cmd_restore_dismissed, db_path, cluster_id=cid)

        assert output["status"] == "ok"
        with location_db.connect(db_path) as conn:
            assert location_db.list_dismissed_clusters(conn) == []

    def test_restore_dismissed_unknown_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        output = _run_cli(_skill().cmd_restore_dismissed, db_path, exit_code=1, cluster_id=9999)
        assert output["status"] == "error"

    def test_place_stats_by_id(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "cafe", 34.0, -118.0, radius_meters=100, category="food")
            _add_pings_at(conn, pid, ["09:00", "09:05", "09:30", "10:00"])
            conn.commit()

        output = _run_cli(_skill().cmd_place_stats, db_path, name=None, id=pid)

        assert output["place_id"] == pid
        assert output["total_visits"] == 1
        assert output["total_duration_min"] == 60

    def test_place_stats_by_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        pid = _add_place(db_path, "home", radius_meters=150, category="home")

        output = _run_cli(_skill().cmd_place_stats, db_path, name="home", id=None)

        assert output["place_id"] == pid
        assert output["total_visits"] == 0

    def test_place_stats_unknown_name(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        output = _run_cli(_skill().cmd_place_stats, db_path, exit_code=1, name="ghost", id=None)
        assert output["status"] == "error"

    def test_place_stats_other_user_cannot_read(self, tmp_path):
        # Per-user isolation is now per-file: alice's place lives in alice's
        # location.db; bob's process points at bob's empty location.db and
        # the place_id miss returns an error.
        alice_db = _init_loc_db(tmp_path / "alice", "location.db")
        pid = _add_place(alice_db, "home", radius_meters=150, category="home")
        bob_db = _init_loc_db(tmp_path / "bob", "location.db")

        output = _run_cli(_skill().cmd_place_stats, bob_db, exit_code=1, name=None, id=pid)
        assert output["status"] == "error"


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

            assert db.get_cached_geocode(conn, "123 Main St") == (34.05, -118.4)

    def test_cache_upsert(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            db.cache_geocode(conn, "123 Main St", 35.0, -119.0)
            conn.commit()

            assert db.get_cached_geocode(conn, "123 Main St") == (35.0, -119.0)


# ===========================================================================
# Attendance helper tests
# ===========================================================================


@pytest.mark.parametrize("location, virtual", [
    ("https://zoom.us/j/12345", True),
    ("meet.google.com/abc-def", True),
    ("Microsoft Teams Meeting", True),
    ("123 Main St, San Francisco", False),
    ("Conference Room B", False),
], ids=["zoom_link", "google_meet", "teams", "physical_location", "conference_room"])
def test_virtual_location_detection(location, virtual):
    from istota.skills.location import _is_virtual_location
    assert _is_virtual_location(location) is virtual


@pytest.mark.parametrize("location, place_names, expected", [
    ("gym", ["gym"], "gym"),
    ("downtown gym", ["Downtown Gym"], "Downtown Gym"),
    ("gym", ["Downtown Gym"], "Downtown Gym"),
    ("The gym on 5th Ave", ["gym"], "gym"),
    ("dentist office", ["gym"], None),
    ("gym", [], None),
], ids=["exact_match", "case_insensitive", "substring_location_in_place",
        "substring_place_in_location", "no_match", "empty_places"])
def test_place_matching(location, place_names, expected):
    from istota.skills.location import _match_place
    places = [{"name": n, "lat": 34.0, "lon": -118.0, "radius_meters": 100} for n in place_names]
    result = _match_place(location, places)
    assert (result["name"] if result else None) == expected


class TestGeocodeLocation:
    def test_cache_hit(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_geocode(conn, "123 Main St", 34.05, -118.4)
            conn.commit()

            assert _geocode_location("123 Main St", conn) == (34.05, -118.4)

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        hit = MagicMock(latitude=37.7749, longitude=-122.4194)
        with db.get_db(db_path) as conn, _patch_nominatim("geocode", returns=hit):
            assert _geocode_location("San Francisco, CA", conn) == (37.7749, -122.4194)
            # Should be cached now
            assert db.get_cached_geocode(conn, "San Francisco, CA") == (37.7749, -122.4194)

    @_needs_geopy
    @pytest.mark.parametrize("nominatim", [
        {"returns": None},
        {"side_effect": Exception("timeout")},
    ], ids=["failure_returns_none", "exception_returns_none"])
    def test_nominatim_miss_returns_none(self, tmp_path, nominatim):
        from istota.skills.location import _geocode_location
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn, _patch_nominatim("geocode", **nominatim):
            assert _geocode_location("nonexistent place xyz", conn) is None


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


_DENTIST = {"name": "dentist office", "lat": 34.05, "lon": -118.4, "radius_meters": 200}


class TestCmdAttendance:
    def _run_attendance(self, tmp_path, events, pings=None, places=None, args_overrides=None):
        """Helper to run cmd_attendance with mocked CalDAV and DB.

        Uses two DBs to mirror production: per-user location.db for
        pings/places, framework istota.db for the global geocode cache.
        """
        from istota.skills.location import cmd_attendance

        loc_db = _init_loc_db(tmp_path, "location.db")
        framework_db = _init_db(tmp_path)  # for geocode_cache
        with location_db.connect(loc_db) as conn:
            for p in (places or []):
                location_db.add_place(
                    conn, p["name"], p["lat"], p["lon"],
                    radius_meters=p.get("radius_meters", 100),
                    category=p.get("category", "other"),
                )
            for ping in (pings or []):
                location_db.insert_ping(
                    conn, ping["timestamp"], ping["lat"], ping["lon"],
                    accuracy=ping.get("accuracy", 5.0),
                )
            conn.commit()

        env = {
            "LOCATION_DB_PATH": str(loc_db),
            "ISTOTA_DB_PATH": str(framework_db),
            "ISTOTA_USER_ID": "alice",
            "CALDAV_URL": "https://cloud.example.com/remote.php/dav",
            "CALDAV_USERNAME": "alice",
            "CALDAV_PASSWORD": "secret",
            "TZ": "America/Los_Angeles",
        }

        args = MagicMock()
        args.date = "2026-03-01"
        args.event = None
        for k, v in (args_overrides or {}).items():
            setattr(args, k, v)

        mock_calendars = [("Personal", "https://cal.example.com/personal")]

        with patch.dict("os.environ", env), \
                patch("istota.skills.calendar.get_caldav_client", return_value=MagicMock()), \
                patch("istota.skills.calendar.list_calendars", return_value=mock_calendars), \
                patch("istota.skills.calendar.get_events", return_value=events):
            return _capture(cmd_attendance, args)

    def test_no_events(self, tmp_path):
        result = self._run_attendance(tmp_path, events=[])
        assert result["date"] == "2026-03-01"
        assert result["events"] == []

    @pytest.mark.parametrize("event_kw", [
        {"location": "123 Main St", "all_day": True},
        {"location": None},
        {"location": "https://zoom.us/j/12345"},
    ], ids=["all_day_event_filtered", "no_location_filtered", "virtual_location_filtered"])
    def test_event_filtered(self, tmp_path, event_kw):
        result = self._run_attendance(tmp_path, events=[_make_calendar_event(**event_kw)])
        assert result["events"] == []

    def test_attendance_confirmed_with_nearby_pings(self, tmp_path):
        events = [_make_calendar_event(uid="dentist1", summary="Dentist", location="dentist office")]
        pings = [
            {"timestamp": "2026-03-01T17:45:00Z", "lat": 34.0501, "lon": -118.4001},  # 10:45 PT, within window
            {"timestamp": "2026-03-01T18:30:00Z", "lat": 34.0502, "lon": -118.3999},  # 11:30 PT, within window
        ]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=[_DENTIST])
        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "place"
        assert ev["nearby_ping_count"] == 2

    def test_no_pings_no_attendance(self, tmp_path):
        events = [_make_calendar_event(summary="Dentist", location="dentist office")]
        result = self._run_attendance(tmp_path, events=events, pings=[], places=[_DENTIST])
        assert len(result["events"]) == 1
        assert result["events"][0]["attended"] is None

    def test_pings_too_far_away(self, tmp_path):
        events = [_make_calendar_event(summary="Dentist", location="dentist office")]
        places = [{**_DENTIST, "radius_meters": 100}]
        pings = [{"timestamp": "2026-03-01T18:00:00Z", "lat": 35.0, "lon": -119.0}]
        result = self._run_attendance(tmp_path, events=events, pings=pings, places=places)
        assert result["events"][0]["attended"] is None

    @_needs_geopy
    def test_ungeocoded_event(self, tmp_path):
        events = [_make_calendar_event(summary="Meeting", location="Some Unknown Place XYZ123")]
        # No places, geocoding will fail
        with _patch_nominatim("geocode", returns=None):
            result = self._run_attendance(tmp_path, events=events)

        assert len(result["events"]) == 1
        ev = result["events"][0]
        assert ev["location_resolved"] is False
        assert ev["attended"] is None

    @_needs_geopy
    def test_geocoded_event_with_attendance(self, tmp_path):
        events = [_make_calendar_event(summary="Dentist", location="123 Main St, LA")]
        # Ping near geocoded location
        pings = [{"timestamp": "2026-03-01T18:00:00Z", "lat": 34.0501, "lon": -118.4001}]

        hit = MagicMock(latitude=34.05, longitude=-118.4)
        with _patch_nominatim("geocode", returns=hit):
            result = self._run_attendance(tmp_path, events=events, pings=pings)

        ev = result["events"][0]
        assert ev["attended"] is True
        assert ev["resolution_source"] == "geocode"

    @pytest.mark.parametrize("uids, selector, field, expected", [
        (("ev1", "ev2"), "dentist", "summary", "Dentist"),
        (("abc123", "def456"), "abc123", "uid", "abc123"),
    ], ids=["by_title", "by_uid"])
    def test_event_filter(self, tmp_path, uids, selector, field, expected):
        events = [
            _make_calendar_event(uid=uids[0], summary="Dentist", location="dentist office"),
            _make_calendar_event(uid=uids[1], summary="Gym", location="gym"),
        ]
        places = [_DENTIST, {"name": "gym", "lat": 34.1, "lon": -118.1, "radius_meters": 100}]
        result = self._run_attendance(
            tmp_path, events=events, places=places, args_overrides={"event": selector},
        )
        assert len(result["events"]) == 1
        assert result["events"][0][field] == expected

    def test_place_radius_used(self, tmp_path):
        """Place with large radius should detect pings that would be outside default 200m."""
        events = [_make_calendar_event(summary="Park", location="big park")]
        places = [{"name": "big park", "lat": 34.05, "lon": -118.4, "radius_meters": 2000}]
        # Ping ~500m away (would fail with 200m default, but passes with 2km)
        pings = [{"timestamp": "2026-03-01T18:00:00Z", "lat": 34.055, "lon": -118.4}]
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
            assert db.get_reverse_geocode(conn, 34.05, -118.25) is None

    def test_store_and_retrieve(self, tmp_path):
        db_path = _init_db(tmp_path)
        data = _geo_row("123 Main St, Los Angeles, CA", neighborhood="Downtown",
                        suburb="Central LA", road="Main St", city="Los Angeles")
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, data)
            conn.commit()

            result = db.get_reverse_geocode(conn, 34.05, -118.25)
            assert {k: result[k] for k in data} == data

    def test_rounding_hits_same_entry(self, tmp_path):
        """Nearby coords (within ~11m) should hit the same cache entry."""
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05001, -118.25002,
                                     _geo_row("Test Place", road="Test Rd", city="Test City"))
            conn.commit()

            # Slightly different coords that round to the same 4-decimal value
            result = db.get_reverse_geocode(conn, 34.05004, -118.25001)
            assert result is not None
            assert result["display_name"] == "Test Place"

    def test_upsert_overwrites(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, _geo_row("Old Name"))
            conn.commit()
            db.cache_reverse_geocode(conn, 34.05, -118.25, _geo_row("New Name", neighborhood="New Hood"))
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
            db.cache_reverse_geocode(conn, 34.05, -118.25, _geo_row(
                "Cached Place", neighborhood="Hood", suburb="Sub", road="Road", city="City",
            ))
            conn.commit()

            result = reverse_geocode(34.05, -118.25, conn)
            assert result["source"] == "cache"
            assert result["display_name"] == "Cached Place"

    @_needs_geopy
    def test_nominatim_called_on_miss(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        hit = _nominatim_hit("456 Oak Ave, Pasadena, CA", road="Oak Ave",
                             neighbourhood="Old Town", suburb="South Pasadena", city="Pasadena")
        with db.get_db(db_path) as conn, _patch_nominatim("reverse", returns=hit):
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
        with db.get_db(db_path) as conn, _patch_nominatim("reverse", returns=None):
            result = reverse_geocode(0.0, 0.0, conn)
            assert result["source"] == "error"
            assert "error" in result

    @_needs_geopy
    def test_nominatim_exception(self, tmp_path):
        from istota.geo import reverse_geocode

        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn, \
                _patch_nominatim("reverse", side_effect=Exception("timeout")):
            result = reverse_geocode(34.05, -118.25, conn)
            assert result["source"] == "error"
            assert "timeout" in result["error"]


# ===========================================================================
# Cluster pings tests (geo.py)
# ===========================================================================


def _cluster(lat, lon, first_ts, last_ts, ping_count):
    return {
        "lat": lat, "lon": lon,
        "first_ts": first_ts, "last_ts": last_ts,
        "ping_count": ping_count,
        "place_name": None, "place_id": None,
    }


class TestFilterTransitClusters:
    """Direct unit tests for filter_transit_clusters() spatial absorption."""

    def test_absorbs_nearby_fragment_into_previous_stop(self):
        """Small cluster within merge radius of previous stop gets absorbed."""
        from istota.geo import filter_transit_clusters

        clusters = [
            # Big stop — survives filtering on its own
            _cluster(34.0836, -118.3101, "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z", 20),
            # Small fragment — same location, indoor GPS gap
            _cluster(34.0837, -118.3100, "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z", 2),
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
            _cluster(34.0836, -118.3101, "2026-04-08T19:07:00Z", "2026-04-08T19:21:00Z", 20),
            # Small fragment at a different location (~1km away)
            _cluster(34.0920, -118.3101, "2026-04-08T19:27:00Z", "2026-04-08T19:28:00Z", 2),
        ]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 1
        assert stops[0]["ping_count"] == 20  # not absorbed
        assert transit == 2

    def test_no_previous_stop_to_absorb_into(self):
        """First cluster is small with no preceding stop — discarded normally."""
        from istota.geo import filter_transit_clusters

        clusters = [_cluster(34.0836, -118.3101, "2026-04-08T19:07:00Z", "2026-04-08T19:08:00Z", 2)]
        stops, transit = filter_transit_clusters(clusters)
        assert len(stops) == 0
        assert transit == 2


def _stop(location, lat, lon, first_ts, last_ts, ping_count,
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


class TestMergeConsecutiveStops:
    """Direct unit tests for merge_consecutive_stops() spatial proximity merge."""

    def test_merges_nearby_unnamed_stops_with_different_names(self):
        """Two consecutive stops ~20m apart with different reverse-geocoded names
        should merge (ISSUE-047 bug A)."""
        from istota.geo import merge_consecutive_stops

        stops = [
            _stop("East Live Oak Drive", 34.1086, -118.3099,
                  "2026-04-10T01:58:00Z", "2026-04-10T03:56:00Z", 33),
            _stop("Tryon Road", 34.1087, -118.3100,
                  "2026-04-10T04:09:00Z", "2026-04-10T04:41:00Z", 11),
            _stop("East Live Oak Drive", 34.1086, -118.3099,
                  "2026-04-10T05:05:00Z", "2026-04-10T05:16:00Z", 18),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["ping_count"] == 62
        # Should keep the name from the longest stop
        assert merged[0]["location"] == "East Live Oak Drive"

    @pytest.mark.parametrize("stops", [
        pytest.param([
            _stop("Elm Street", 34.05, -118.25, "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            _stop("Oak Avenue", 34.06, -118.25, "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15),
        ], id="distant_unnamed_stops"),
        pytest.param([
            _stop("Home", 34.1025, -118.3059, "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20,
                  location_source="saved_place"),
            _stop("Neighbor", 34.1026, -118.3060, "2026-04-10T11:30:00Z", "2026-04-10T12:00:00Z", 15,
                  location_source="saved_place"),
        ], id="nearby_saved_places_not_proximity_merged"),
        pytest.param([
            _stop("Road A", 34.1086, -118.3099, "2026-04-10T10:00:00Z", "2026-04-10T11:00:00Z", 20),
            _stop("Road B", 34.1087, -118.3100, "2026-04-10T12:00:00Z", "2026-04-10T13:00:00Z", 15,
                  transit_before=5),
        ], id="nearby_but_separated_by_transit"),
    ])
    def test_does_not_merge(self, stops):
        from istota.geo import merge_consecutive_stops

        assert len(merge_consecutive_stops(stops)) == 2

    def test_proximity_merge_keeps_longer_stop_name(self):
        """When merging by proximity, the name from the longer stop is kept."""
        from istota.geo import merge_consecutive_stops

        stops = [
            _stop("Short Road", 34.1086, -118.3099,
                  "2026-04-10T10:00:00Z", "2026-04-10T10:10:00Z", 5),
            _stop("Main Boulevard", 34.1087, -118.3100,
                  "2026-04-10T10:15:00Z", "2026-04-10T12:00:00Z", 30),
        ]
        merged = merge_consecutive_stops(stops)
        assert len(merged) == 1
        assert merged[0]["location"] == "Main Boulevard"


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
        assert [c["ping_count"] for c in result] == [1, 1]

    def test_centroid_drift_splits_route(self):
        """Many pings drifting slowly along a road should NOT merge into one cluster.

        Simulates riding ~500m along a street with 120 pings (~4m each).
        Each ping is close to the drifting centroid but far from the origin.
        The origin anchor should force a split.
        """
        from istota.geo import cluster_pings

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
        # A + B1 + B2, then C split off by origin anchor
        assert [c["ping_count"] for c in result] == [3, 1]

    def test_time_gap_splits_cluster(self):
        """Pings at the same location but >5 min apart should split."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": f"2026-04-03T10:{m}:00Z"}
            for m in ("00", "01", "11", "12")  # 10-minute gap after 10:01
        ]
        result = cluster_pings(pings, max_gap_seconds=300)
        assert [c["ping_count"] for c in result] == [2, 2]

    def test_stationary_pings_cluster_normally(self):
        """Pings at the same spot with no time gaps stay in one cluster."""
        from istota.geo import cluster_pings

        pings = [
            {"lat": 34.05, "lon": -118.25, "timestamp": f"2026-04-03T10:{i:02d}:00Z"}
            for i in range(20)
        ]
        result = cluster_pings(pings, radius_m=200)
        assert [c["ping_count"] for c in result] == [20]


def _dp(time, accuracy, activity_type, lat=34.10, lon=-118.30):
    return {"timestamp": f"2026-04-28T{time}Z", "lat": lat, "lon": lon,
            "accuracy": accuracy, "activity_type": activity_type}


class TestDedupeNearDuplicatePings:
    """Tests for dedupe_near_duplicate_pings() — strips dual-source artifacts.

    The phone (Overland/iOS) sometimes reports two location fixes within a few
    seconds: one high-accuracy GPS fix and one low-accuracy cell/Wi-Fi fix
    anchored elsewhere. The cell/Wi-Fi ping typically has activity_type=None.
    See ISSUE-059.
    """

    @pytest.mark.parametrize("pings, kept", [
        pytest.param([], [], id="empty_input"),
        pytest.param([_dp("03:23:53", 6.0, "walking", 34.1, -118.3)], [0],
                     id="single_ping_passes_through"),
        pytest.param([_dp("03:23:00", 60.0, None), _dp("03:23:10", 6.0, "walking")], [0, 1],
                     id="pings_more_than_5s_apart_both_kept"),
        # The most common case: cell/Wi-Fi ping has activity_type=None.
        pytest.param([_dp("03:23:42", 63.0, "walking", 34.10434, -118.30830),
                      _dp("03:23:43", 56.0, None, 34.10274, -118.30598)], [0],
                     id="one_set_one_null_drops_null"),
        pytest.param([_dp("03:23:42", 56.0, None, 34.10274, -118.30598),
                      _dp("03:23:43", 63.0, "walking", 34.10434, -118.30830)], [1],
                     id="one_null_one_set_drops_null_regardless_of_order"),
        # activity_type wins over accuracy — confirmed by issue example
        # 20061/20062: null had 40m, walking had 55m, but walking is the real fix.
        pytest.param([_dp("03:27:21", 40.0, None, 34.10456, -118.30962),
                      _dp("03:27:21", 55.0, "walking", 34.10436, -118.30992)], [1],
                     id="one_set_keeps_tagged_even_if_accuracy_worse"),
        pytest.param([_dp("03:23:42", 55.0, None), _dp("03:23:43", 14.0, None)], [1],
                     id="both_null_picks_better_accuracy"),
        # No way to distinguish — preserve raw data rather than guess.
        pytest.param([_dp("03:23:42", 30.0, None), _dp("03:23:43", 30.0, None, 34.11, -118.31)],
                     [0, 1], id="both_null_equal_accuracy_keeps_both"),
        # Per design: rare case (18/204 in prod), keep both rather than drop a real fix.
        pytest.param([_dp("03:23:42", 10.0, "driving"),
                      _dp("03:23:43", 10.0, "driving", 34.11, -118.31)],
                     [0, 1], id="both_set_equal_accuracy_keeps_both"),
        pytest.param([_dp("03:23:42", 14.0, "walking"), _dp("03:23:43", 5.0, "walking")], [1],
                     id="both_set_unequal_accuracy_keeps_better"),
        # The first two collapse to walking; the third is 10s after the second,
        # within 5s of nothing in the kept set, so it stays.
        pytest.param([_dp("03:23:42", 63.0, "walking", 34.10434, -118.30830),
                      _dp("03:23:43", 56.0, None, 34.10274, -118.30598),
                      _dp("03:23:53", 6.0, "walking", 34.10428, -118.30889)], [0, 2],
                     id="chain_of_three_within_window"),
        pytest.param([_dp("03:23:00", 60.0, None), _dp("03:23:05", 6.0, "walking")], [1],
                     id="window_boundary_5s_inclusive"),
        pytest.param([_dp("03:23:00", 60.0, None), _dp("03:23:06", 6.0, "walking")], [0, 1],
                     id="window_boundary_just_over_5s_keeps_both"),
        # The 2026-04-27 issue example: real walking + cell/Wi-Fi anchor + real
        # walking + a place-matched pair. One walking ping survives per
        # timestamp cluster and the cell anchors are dropped.
        pytest.param([_dp("03:23:42", 63.0, "walking", 34.1043368, -118.3082973),
                      _dp("03:23:43", 56.0, None, 34.10274185, -118.30598),
                      _dp("03:23:53", 6.0, "walking", 34.104277, -118.3088875),
                      _dp("03:27:21", 40.0, None, 34.10456, -118.30962),
                      _dp("03:27:21", 55.0, "walking", 34.10436, -118.30992)], [0, 2, 4],
                     id="zigzag_walk_collapses_cleanly"),
        # Both null-activity and one lacks accuracy: can't compare, keep both.
        pytest.param([_dp("03:23:42", None, None), _dp("03:23:43", 14.0, None)], [0, 1],
                     id="missing_accuracy_treated_as_tie"),
    ])
    def test_dedupe(self, pings, kept):
        from istota.geo import dedupe_near_duplicate_pings

        assert dedupe_near_duplicate_pings(pings) == [pings[i] for i in kept]

    def test_does_not_mutate_input(self):
        from istota.geo import dedupe_near_duplicate_pings

        pings = [_dp("03:23:42", 60.0, None), _dp("03:23:43", 6.0, "walking")]
        dedupe_near_duplicate_pings(pings)
        assert len(pings) == 2


def _gym_ping(ts, lat=34.1000, **kw):
    ping = {"lat": lat, "lon": -118.3000, "timestamp": ts}
    ping.update(kw)
    return ping


def _tagged_gym(ts):
    return _gym_ping(ts, place_id=7, place_name="Gym")


class TestClusterPlaceAttribution:
    """Place-aware clustering (option 6, ISSUE-062): a cluster is attributed to
    a place by counting per-ping place_id matches, not by the centroid. This
    sidesteps centroid contamination from walking legs and crosswalk waits and
    weeds out drive-by grazing pings via a minimum-count threshold.
    """

    @pytest.mark.parametrize("tags, place_id, place_name", [
        # Drive-by: one grazing tagged ping must not promote the cluster to a
        # place — that's the phantom-stop scenario.
        ([None, (7, "X")], None, None),
        # Two grazing pings still below threshold — slow drive-by territory.
        ([(7, "X"), (7, "X"), None], None, None),
        # At MIN_PLACE_PINGS (3), the cluster takes on the place_id.
        ([(7, "X"), (7, "X"), (7, "X")], 7, "X"),
        # Different place_ids: the most-counted one wins, provided it meets the threshold.
        ([(5, "Y"), (6, "Z"), (6, "Z"), (6, "Z")], 6, "Z"),
        ([None, None], None, None),
    ], ids=["single_tagged_ping_does_not_anchor_place", "two_tagged_pings_below_threshold",
            "three_tagged_pings_meets_threshold", "majority_wins_when_multiple_places",
            "no_place_when_no_pings_have_place"])
    def test_attribution_threshold(self, tags, place_id, place_name):
        from istota.geo import cluster_pings

        pings = []
        for i, tag in enumerate(tags):
            ping = {"lat": 34.10 + 0.00001 * i, "lon": -118.30 + 0.00001 * i,
                    "timestamp": f"2026-04-28T03:23:{10 * i:02d}Z"}
            if tag:
                ping["place_id"], ping["place_name"] = tag
            pings.append(ping)

        result = cluster_pings(pings, radius_m=250)
        assert len(result) == 1
        assert result[0]["place_id"] == place_id
        assert result[0]["place_name"] == place_name

    def test_lazy_acres_scenario(self):
        """Walking legs contaminate the centroid past the place radius, but the
        17 stationary pings inside the geofence still drive attribution.
        Reproduces the ISSUE-062 case: real-time webhook fired correct
        arrival/departure, day-summary should match."""
        from istota.geo import cluster_pings

        # 5 walk-in pings drifting east toward the store, no place_id
        # 17 stationary pings at the store (within the 75m geofence)
        # 19 walk-out pings drifting east, no place_id
        # Total: 41 pings, 17 tagged with Lazy Acres
        pings = []
        for i in range(5):
            pings.append({
                "lat": 34.1042, "lon": -118.3085 - 0.0001 * (5 - i),
                "timestamp": f"2026-04-28T03:38:{i * 10:02d}Z",
            })
        for i in range(17):
            pings.append({
                "lat": 34.1044, "lon": -118.3097,
                "timestamp": f"2026-04-28T03:39:{i * 10:02d}Z" if i < 6 else f"2026-04-28T03:{40 + (i - 6) // 6:02d}:{((i - 6) % 6) * 10:02d}Z",
                "place_id": 1398, "place_name": "Lazy Acres",
            })
        for i in range(19):
            pings.append({
                "lat": 34.1041, "lon": -118.3085 - 0.0001 * i,
                "timestamp": f"2026-04-28T03:{43 + i // 6:02d}:{(i % 6) * 10:02d}Z",
            })

        result = cluster_pings(pings, radius_m=250)
        # All 41 pings land in one cluster (intentional — that's the bug).
        # The fix: even though the centroid is contaminated, the 17 tagged
        # pings still attribute the cluster to Lazy Acres.
        attributed = [c for c in result if c["place_id"] == 1398]
        assert attributed, "expected at least one cluster attributed to Lazy Acres"

    def test_stop_ends_when_reporting_resumes_outside_place(self):
        """A quiet tracker does not turn its last stationary ping into departure."""
        from istota.geo import cluster_pings

        pings = [
            *[_tagged_gym(f"2026-08-26T14:{t}Z") for t in ("24:00", "29:00", "34:00", "38:33")],
            _gym_ping("2026-08-26T15:45:17Z", activity_type="driving"),
        ]

        result = cluster_pings(pings)

        assert result[0]["place_name"] == "Gym"
        assert result[0]["last_ts"] == "2026-08-26T15:45:17Z"

    def test_stop_end_subtracts_travel_time_to_distant_closing_ping(self):
        """A distant resume ping bounds departure before the observation time."""
        from istota.geo import cluster_pings

        pings = [
            *[_tagged_gym(f"2026-08-26T14:{m}:00Z") for m in ("00", "05", "10")],
            _gym_ping("2026-08-26T15:10:00Z", lat=34.1720, speed=20.0, activity_type="driving"),
        ]

        result = cluster_pings(pings)

        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        closing_ping = datetime.fromisoformat("2026-08-26T15:10:00+00:00")
        travel_seconds = (closing_ping - departure).total_seconds()
        assert 6 * 60 < travel_seconds < 7 * 60

    def test_stop_end_extension_is_capped(self):
        """A dead tracker cannot turn the next day's first ping into a day-long stop."""
        from istota.geo import MAX_STOP_EXTENSION_SECONDS, cluster_pings

        pings = [
            *[_tagged_gym(f"2026-08-26T14:{m}:00Z") for m in ("00", "05", "10")],
            _gym_ping("2026-08-27T08:00:00Z"),
        ]

        result = cluster_pings(pings)

        last_inside = datetime.fromisoformat("2026-08-26T14:10:00+00:00")
        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        assert (departure - last_inside).total_seconds() == MAX_STOP_EXTENSION_SECONDS

    def test_first_outside_ping_inside_cluster_closes_stop(self):
        """Nearby exit pings close a placed stop even when they do not split it."""
        from istota.geo import cluster_pings

        pings = [
            *[_tagged_gym(f"2026-08-26T14:{m}:00Z") for m in ("00", "01", "02")],
            _gym_ping("2026-08-26T14:03:00Z", lat=34.1009, speed=10.0, activity_type="driving"),
            _gym_ping("2026-08-26T14:04:00Z", lat=34.1012, speed=10.0, activity_type="driving"),
        ]

        result = cluster_pings(pings)

        assert len(result) == 1
        departure = datetime.fromisoformat(result[0]["last_ts"].replace("Z", "+00:00"))
        first_outside = datetime.fromisoformat("2026-08-26T14:03:00+00:00")
        assert 9 < (first_outside - departure).total_seconds() < 11


# ===========================================================================
# reverse-geocode CLI command tests
# ===========================================================================


class TestCmdReverseGeocode:
    def test_returns_json(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.cache_reverse_geocode(conn, 34.05, -118.25, _geo_row(
                "Test Place", neighborhood="Hood", suburb="Sub", road="Road", city="City",
            ))
            conn.commit()

        result = _run_cli(_skill().cmd_reverse_geocode, db_path, lat=34.05, lon=-118.25)
        assert result["source"] == "cache"
        assert result["display_name"] == "Test Place"

    @_needs_geopy
    def test_nominatim_fallback(self, tmp_path):
        db_path = _init_db(tmp_path)
        hit = _nominatim_hit("789 Pine St", road="Pine St", city="Glendale")

        with _patch_nominatim("reverse", returns=hit):
            result = _run_cli(_skill().cmd_reverse_geocode, db_path, lat=34.15, lon=-118.14)

        assert result["source"] == "nominatim"
        assert result["road"] == "Pine St"


# ===========================================================================
# day-summary CLI command tests
# ===========================================================================


def _run_day_summary(tmp_path, pings=None, places=None,
                     date="2026-03-08", tz="America/Los_Angeles",
                     nominatim_results=None):
    """Run cmd_day_summary with a test DB and a mocked Nominatim.

    Uses two DBs to mirror production: per-user location.db for
    pings/places, framework istota.db for reverse_geocode_cache.
    """
    from istota.skills.location import cmd_day_summary

    loc_db = _init_loc_db(tmp_path, "location.db")
    framework_db = _init_db(tmp_path)  # for reverse_geocode_cache
    with location_db.connect(loc_db) as conn:
        for p in (places or []):
            location_db.add_place(
                conn, p["name"], p["lat"], p["lon"],
                radius_meters=p.get("radius_meters", 100),
                category=p.get("category", "other"),
            )
        for ping in (pings or []):
            location_db.insert_ping(
                conn, ping["timestamp"], ping["lat"], ping["lon"],
                accuracy=ping.get("accuracy", 5.0),
                speed=ping.get("speed"),
                activity_type=ping.get("activity_type"),
                place_id=ping.get("place_id"),
                wifi_zone=ping.get("wifi_zone", False),
                source=ping.get("source", "overland"),
            )
        conn.commit()

    env = {
        "LOCATION_DB_PATH": str(loc_db),
        "ISTOTA_DB_PATH": str(framework_db),
        "ISTOTA_USER_ID": "alice",
        "TZ": tz,
    }
    args = MagicMock()
    args.date = date
    args.tz = tz

    nominatim = ({"side_effect": nominatim_results} if nominatim_results
                 else {"returns": None})
    with patch.dict("os.environ", env, clear=False), _patch_nominatim("reverse", **nominatim):
        return _capture(cmd_day_summary, args)


def _pings_at(lat, lon, times, day="2026-03-08", **kw):
    """One ping per ``HH:MM`` in ``times`` at a single spot."""
    return [{"timestamp": f"{day}T{t}:00Z", "lat": lat, "lon": lon, **kw} for t in times]


@_needs_geopy
class TestCmdDaySummary:
    def test_no_pings_empty_stops(self, tmp_path):
        result = _run_day_summary(tmp_path)
        assert result["date"] == "2026-03-08"
        assert result["stops"] == []
        assert result["ping_count"] == 0

    def test_single_stop_at_saved_place(self, tmp_path):
        """Pings at a saved place should use the place name."""
        # March 8 in PST = UTC 2026-03-08T08:00:00Z to 2026-03-09T08:00:00Z.
        # Pings must be spaced within cluster_pings.max_gap_seconds (300s).
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.05, "lon": -118.25, "place_id": 1},
            {"timestamp": "2026-03-08T16:02:00Z", "lat": 34.0501, "lon": -118.2501, "place_id": 1},
            {"timestamp": "2026-03-08T16:04:00Z", "lat": 34.0502, "lon": -118.2502, "place_id": 1},
        ]
        result = _run_day_summary(tmp_path, pings=pings, places=places)
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
        result = _run_day_summary(tmp_path, pings=pings)
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
        result = _run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "cafe"
        assert result["stops"][0]["location_source"] == "saved_place_proximity"

    def test_reverse_geocode_fallback(self, tmp_path):
        """When no place match, reverse geocode should be used."""
        hit = _nominatim_hit("789 Elm St, Burbank, CA", road="Elm St",
                             suburb="Magnolia Park", city="Burbank")
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            {"timestamp": "2026-03-08T16:05:00Z", "lat": 34.1801, "lon": -118.3301},
            {"timestamp": "2026-03-08T16:10:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = _run_day_summary(tmp_path, pings=pings, nominatim_results=[hit])
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
        result = _run_day_summary(tmp_path, pings=pings, places=places)
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
        result = _run_day_summary(tmp_path, pings=pings, places=places)
        home_stops = [s for s in result["stops"] if s["location"] == "Home"]
        assert len(home_stops) == 2, (
            f"Expected 2 Home stops (left and returned), got {len(home_stops)}: {result['stops']}"
        )

    def test_same_location_merged_after_phone_sleep(self, tmp_path):
        """Home with phone sleep gap (no transit) should merge into one stop."""
        places = [{"name": "Home", "lat": 34.1025, "lon": -118.3059, "radius_meters": 100}]
        # Each side needs ≥3 pings tagged with the place_id to get attributed
        # (MIN_PLACE_PINGS=3); ping spacing must be ≤300s for cluster_pings to
        # keep them in one cluster on each side of the 2-hour sleep gap.
        pings = _pings_at(34.1025, -118.3059, ["00:48", "00:50", "00:52", "02:46", "02:48", "02:50"],
                          day="2026-03-09", place_id=1)
        result = _run_day_summary(tmp_path, pings=pings, places=places)
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
        result = _run_day_summary(tmp_path, pings=pings)
        # All pings are at the same location — should be one stop
        assert len(result["stops"]) == 1, (
            f"Expected 1 stop (indoor GPS gaps), got {len(result['stops'])}: {result['stops']}"
        )
        # The stop should span the full visit
        assert result["stops"][0]["ping_count"] == len(pings)

    def test_duration_minutes_in_output(self, tmp_path):
        """Each stop should include a pre-computed duration_minutes field (ISSUE-047 bug B)."""
        places = [{"name": "home", "lat": 34.05, "lon": -118.25, "radius_meters": 150}]
        # 2 hours at home (16:00-18:00 UTC on March 8 = within PST day).
        # Pings every 5 minutes keep them in a single cluster
        # (cluster_pings.max_gap_seconds=300).
        pings = [
            {
                "timestamp": f"2026-03-08T{16 + (5 * i) // 60:02d}:{(5 * i) % 60:02d}:00Z",
                "lat": 34.05 + i * 0.00001,
                "lon": -118.25,
                "place_id": 1,
            }
            for i in range(25)  # 0, 5, 10, …, 120 minutes — 25 pings
        ]
        result = _run_day_summary(tmp_path, pings=pings, places=places)
        assert len(result["stops"]) == 1
        assert result["stops"][0]["duration_minutes"] == 120

    def test_duration_uses_first_ping_after_stationary_reporting_gap(self, tmp_path):
        """Day summary counts the quiet part of a saved-place visit."""
        places = [{"name": "Gym", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            *_pings_at(34.10, -118.30, ["14:24", "14:29", "14:34"], place_id=1),
            {"timestamp": "2026-03-08T14:38:33Z", "lat": 34.10, "lon": -118.30, "place_id": 1},
            {"timestamp": "2026-03-08T15:45:17Z", "lat": 34.10, "lon": -118.30,
             "activity_type": "driving"},
        ]

        result = _run_day_summary(tmp_path, pings=pings, places=places)

        assert len(result["stops"]) == 1
        assert result["stops"][0]["location"] == "Gym"
        assert result["stops"][0]["duration_minutes"] == 81
        assert result["stops"][0]["departed"] == "08:45"

    def test_duration_uses_recorded_speed_for_distant_closing_ping(self, tmp_path):
        places = [{"name": "Gym", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            *_pings_at(34.10, -118.30, ["14:00", "14:05", "14:10"], place_id=1),
            *_pings_at(34.172, -118.30, ["15:10"], speed=20.0, activity_type="driving"),
        ]

        result = _run_day_summary(tmp_path, pings=pings, places=places)

        assert result["stops"][0]["duration_minutes"] == 63

    def test_closing_ping_after_local_midnight_ends_stop(self, tmp_path):
        places = [{"name": "Home", "lat": 34.10, "lon": -118.30, "radius_meters": 150}]
        pings = [
            *_pings_at(34.10, -118.30, ["06:40", "06:45", "06:50"], day="2026-03-09", place_id=1),
            *_pings_at(34.10, -118.30, ["07:10"], day="2026-03-09", activity_type="driving"),
        ]

        result = _run_day_summary(tmp_path, pings=pings, places=places)

        assert result["ping_count"] == 3
        assert result["stops"][0]["ping_count"] == 3
        assert result["stops"][0]["duration_minutes"] == 30
        assert result["stops"][0]["departed"] == "00:10"

    def test_duration_minutes_for_nominatim_stop(self, tmp_path):
        """duration_minutes should work for reverse-geocoded stops too."""
        hit = _nominatim_hit("Test Place", suburb="TestVille")

        # 30-minute stop with pings close enough to avoid cluster splitting
        # (max_gap_seconds=300, so keep gaps under 5 min)
        pings = [
            {"timestamp": "2026-03-08T16:00:00Z", "lat": 34.18, "lon": -118.33},
            *_pings_at(34.1801, -118.3301, [f"16:{m:02d}" for m in range(4, 29, 4)]),
            {"timestamp": "2026-03-08T16:30:00Z", "lat": 34.1802, "lon": -118.3302},
        ]
        result = _run_day_summary(tmp_path, pings=pings, nominatim_results=[hit])
        assert len(result["stops"]) == 1
        assert result["stops"][0]["duration_minutes"] == 30

    def test_nearby_stops_with_different_geocoded_names_merge(self, tmp_path):
        """ISSUE-047 scenario: GPS drift causes different road names for same location.

        Three clusters at nearly identical coordinates get different reverse-geocoded
        names. They should merge into a single stop via proximity check.
        """
        results = [
            _nominatim_hit(f"{road}, Los Feliz, CA", road=road, suburb="Los Feliz")
            for road in ["East Live Oak Drive", "Tryon Road", "East Live Oak Drive"]
        ]

        # Three clusters ~110m apart (within 150m merge radius), separated by
        # time gaps that cause cluster splitting. Each cluster is big enough
        # (6+ min dwell, 3+ pings) to independently survive transit filtering,
        # so they reach merge_consecutive_stops as separate stops.
        # Coords differ enough that geocode cache gives different results.
        pings = [
            # Cluster 1: "East Live Oak Drive" — 5 pings over 10 min
            *_pings_at(34.1086, -118.3099, [f"01:{i*2:02d}" for i in range(5)], day="2026-03-09"),
            # > 5min gap → Cluster 2: "Tryon Road" — ~110m from cluster 1
            *_pings_at(34.1096, -118.3099, [f"01:{20+i*2:02d}" for i in range(5)], day="2026-03-09"),
            # > 5min gap → Cluster 3: "East Live Oak Drive" again
            *_pings_at(34.1086, -118.3099, [f"01:{40+i*2:02d}" for i in range(5)], day="2026-03-09"),
        ]
        result = _run_day_summary(tmp_path, pings=pings, nominatim_results=results)
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

    @pytest.mark.parametrize("accuracy, assigned", [
        (1336, False),
        (15, True),
        # Missing accuracy shouldn't cause us to drop the ping silently.
        (None, True),
    ], ids=["low_accuracy_ping_not_assigned_to_place", "good_accuracy_ping_is_assigned",
            "null_accuracy_passes"])
    def test_accuracy_gate(self, tmp_path, monkeypatch, accuracy, assigned):
        from istota import webhook_receiver as wr
        _location_config(monkeypatch)

        properties = {"timestamp": "2026-04-21T08:20:00Z"}
        if accuracy is not None:
            properties["horizontal_accuracy"] = accuracy
        feat = {
            "geometry": {"type": "Point", "coordinates": [139.741, 35.629]},
            "properties": properties,
        }

        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741, radius_meters=200)
            wr._process_feature(conn, feat, location_db.get_places(conn))
            conn.commit()

            pings = location_db.get_pings(conn)
            assert len(pings) == 1
            assert pings[0].place_id == (pid if assigned else None)
            if not assigned:
                assert location_db.get_open_visit(conn) is None


@_needs_fastapi
class TestDwellBasedExit:
    """Brief GPS flicker out of place radius must not close an open visit."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        _location_config(monkeypatch)

    def _process(self, conn, place_id, place, timestamp):
        from istota.webhook_receiver import _update_state_machine
        ping_id = location_db.insert_ping(
            conn, timestamp, 0.0, 0.0, accuracy=10.0,
            place_id=place_id,
        )
        _update_state_machine(conn, ping_id, place_id, place, timestamp)
        return ping_id

    def _visit_home(self, tmp_path, steps):
        """Feed ``(HH:MM:SS, at_home)`` steps for 2026-04-21; return (visits, state)."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "home", 35.629, 139.741)
            place = location_db.get_place_by_name(conn, "home")
            for time, at_home in steps:
                self._process(conn, pid if at_home else None, place if at_home else None,
                              f"2026-04-21T{time}Z")
            return location_db.get_visits(conn), location_db.get_location_state(conn)

    def test_flicker_does_not_close_visit(self, tmp_path):
        visits, _ = self._visit_home(tmp_path, [
            ("10:00:00", True), ("10:00:30", True),
            ("10:01:00", False), ("10:02:00", True), ("10:03:00", False),
            ("10:04:00", True), ("10:05:00", False), ("10:06:00", True),
        ])
        assert len(visits) == 1, "Flicker should not create extra visits"
        assert visits[0].exited_at is None, "Visit should still be open"

    def test_continuous_away_closes_after_threshold(self, tmp_path):
        visits, _ = self._visit_home(tmp_path, [
            ("10:00:00", True), ("10:05:00", True),
            ("10:10:00", False), ("10:12:00", False), ("10:14:00", False), ("10:16:00", False),
        ])
        assert len(visits) == 1
        assert visits[0].exited_at == "2026-04-21T10:10:00Z", (
            "Exited_at should be the first away ping, not the last"
        )
        assert visits[0].duration_sec == 600

    def test_away_then_return_extends_visit(self, tmp_path):
        visits, state = self._visit_home(tmp_path, [
            ("10:00:00", True), ("10:05:00", True),
            ("10:06:00", False), ("10:07:30", False),
            ("10:08:00", True), ("10:20:00", True),
        ])
        assert len(visits) == 1
        assert visits[0].exited_at is None, "Visit should still be open"
        assert state.exit_started_at is None

    def test_direct_place_to_place_closes_old_opens_new(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            (pid_h, home), (pid_g, gym) = _home_and_gym(conn)

            self._process(conn, pid_h, home, "2026-04-21T10:00:00Z")
            self._process(conn, pid_h, home, "2026-04-21T10:05:00Z")
            self._process(conn, pid_g, gym, "2026-04-21T10:06:00Z")
            self._process(conn, pid_g, gym, "2026-04-21T10:07:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 2
            home_visit = [v for v in visits if v.place_name == "home"][0]
            gym_visit = [v for v in visits if v.place_name == "gym"][0]
            assert home_visit.exited_at is not None
            assert gym_visit.exited_at is None


_APR21 = ("2026-04-21T00:00:00Z", "2026-04-22T00:00:00Z")


class TestReconcileVisits:
    def _ping(self, conn, ts, place_id):
        location_db.insert_ping(
            conn, ts, 0.0, 0.0, accuracy=10.0, place_id=place_id,
        )

    def _reconcile(self, conn, since, until, **kw):
        n = location_db.reconcile_visits(
            conn, since=since, until=until,
            grace_minutes=10.0, min_pings=3, min_dwell_sec=60, **kw,
        )
        conn.commit()
        return n

    def _home(self, conn):
        return location_db.add_place(conn, "home", 35.629, 139.741)

    def test_reconciles_fragmented_visit_into_one(self, tmp_path):
        """The Shinagawa case: flicker split a single stay into many short segments."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # 15 pings mostly at place, a handful briefly outside
            for m in range(0, 30, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            # sprinkle a few unassigned pings in between — gaps < grace
            for ts in ("2026-04-21T10:05:30Z", "2026-04-21T10:13:30Z", "2026-04-21T10:19:30Z"):
                self._ping(conn, ts, None)
            conn.commit()

            assert self._reconcile(conn, *_APR21) == 1
            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"
            assert visits[0].ping_count == 15

    def test_same_place_reporting_gaps_do_not_split_visit(self, tmp_path):
        """ISSUE-329: silence is not evidence that the user left a place."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "gym", 34.0, -118.0)
            minutes = [
                "09:00", "09:01", "09:02", "09:04", "09:06", "09:07", "09:08", "09:09",
                "09:33", "09:34", "09:36", "09:39", "09:41", "09:44", "09:47", "09:51",
                "10:20", "10:21", "10:22", "10:23", "10:24", "10:25", "10:26", "10:27",
                "10:29",
            ]
            for hhmm in minutes:
                self._ping(conn, f"2026-01-10T{hhmm}:00Z", pid)
            conn.commit()

            assert self._reconcile(conn, "2026-01-10T00:00:00Z", "2026-01-11T00:00:00Z") == 1
            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-01-10T09:00:00Z"
            assert visits[0].exited_at == "2026-01-10T10:29:00Z"
            assert visits[0].ping_count == 25

    def test_same_place_merge_replaces_visit_before_window(self, tmp_path):
        """A merged segment must not overlap a preserved pre-fix visit."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "gym", 34.0, -118.0)
            first_id = location_db.open_visit(conn, pid, "gym", "2026-01-10T08:00:00Z")
            location_db.close_visit(conn, first_id, "2026-01-10T08:08:00Z")
            second_id = location_db.open_visit(conn, pid, "gym", "2026-01-10T10:00:00Z")
            location_db.close_visit(conn, second_id, "2026-01-10T10:08:00Z")
            for minute in (0, 4, 8):
                location_db.insert_ping(
                    conn, f"2026-01-10T08:{minute:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=first_id,
                )
                location_db.insert_ping(
                    conn, f"2026-01-10T10:{minute:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=second_id,
                )
            conn.commit()

            self._reconcile(conn, "2026-01-10T09:00:00Z", "2026-01-10T11:00:00Z")

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-01-10T08:00:00Z"
            assert visits[0].exited_at == "2026-01-10T10:08:00Z"
            assert visits[0].ping_count == 6

    def test_filters_walkby(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # Only 2 pings at place — below min_pings=3
            self._ping(conn, "2026-04-21T10:00:00Z", pid)
            self._ping(conn, "2026-04-21T10:01:00Z", pid)
            conn.commit()

            assert self._reconcile(conn, *_APR21) == 0
            assert location_db.get_visits(conn) == []

    def test_splits_on_different_place(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid_a = location_db.add_place(conn, "home", 34.0, -118.0)
            pid_b = location_db.add_place(conn, "gym", 34.1, -118.1)
            for m in range(0, 10, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid_a)
            for m in range(12, 22, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid_b)
            conn.commit()

            assert self._reconcile(conn, *_APR21) == 2
            visits = sorted(location_db.get_visits(conn), key=lambda v: v.entered_at)
            assert [v.place_name for v in visits] == ["home", "gym"]

    def test_preserves_open_visit_outside_window(self, tmp_path):
        """An open visit started before `since` must be left alone."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # Open visit entered before reconcile window
            vid = location_db.open_visit(conn, pid, "home", "2026-04-20T23:00:00Z")
            for m in range(0, 10, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            self._reconcile(conn, *_APR21)

            # The open visit must still exist and be open
            open_ones = [v for v in location_db.get_visits(conn) if v.exited_at is None]
            assert len(open_ones) == 1
            assert open_ones[0].id == vid

    def test_accuracy_filter_drops_bad_pings(self, tmp_path):
        """Historical pings with accuracy > threshold are treated as unassigned."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # One early bad-accuracy ping pinned to the place (like the 1336m Shinagawa case)
            location_db.insert_ping(conn, "2026-04-21T08:00:00Z", 35.629, 139.741,
                accuracy=1200.0, place_id=pid,
            )
            # Real visit starts later with good pings
            for m in range(30, 50, 2):
                location_db.insert_ping(conn, f"2026-04-21T08:{m:02d}:00Z", 35.629, 139.741,
                    accuracy=10.0, place_id=pid,
                )
            conn.commit()

            # Without filter: the bad ping would anchor a visit starting at 08:00
            self._reconcile(conn, *_APR21, accuracy_threshold_m=100.0)

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T08:30:00Z", (
                "Bad-accuracy ping should not have anchored the visit's entered_at"
            )
            assert visits[0].exited_at == "2026-04-21T08:48:00Z"

    def test_replaces_stale_closed_visits_in_window(self, tmp_path):
        """Existing closed visits in the window are dropped before re-derivation."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # Seed with an incorrect, short closed visit
            stale_id = location_db.open_visit(conn, pid, "home", "2026-04-21T10:05:00Z")
            location_db.close_visit(conn, stale_id, "2026-04-21T10:07:00Z")
            # Pings showing the true longer stay
            for m in range(0, 30, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            self._reconcile(conn, *_APR21)

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].id != stale_id  # stale row deleted
            assert visits[0].entered_at == "2026-04-21T10:00:00Z"
            assert visits[0].exited_at == "2026-04-21T10:28:00Z"

    def test_idempotent_across_sliding_windows(self, tmp_path):
        """ISSUE-064: sliding window must not accumulate phantom visits.

        The daemon runs reconcile_visits every minute over a sliding window.
        When `since` advances past a visit's first ping but the last ping is
        still in window, prior runs must be cleaned up — not duplicated.
        """
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "lazy_acres", 32.78, -117.18)
            # 20 sparse pings over 11 minutes (Lazy Acres pattern)
            ping_times = [
                "38:00", "38:30", "39:00", "39:30", "40:15", "40:50", "41:30",
                "42:10", "42:50", "43:30", "44:10", "44:55", "45:40", "46:20",
                "47:00", "47:40", "48:10", "48:40", "49:00", "49:30",
            ]
            for mmss in ping_times:
                self._ping(conn, f"2026-04-29T03:{mmss}Z", pid)
            conn.commit()

            # Three reconciler runs with `since` sliding past the visit's
            # first ping but `until` still after the last ping.
            for since, until in [
                ("2026-04-29T03:30:00Z", "2026-04-29T03:50:00Z"),
                ("2026-04-29T03:40:00Z", "2026-04-29T03:56:00Z"),
                ("2026-04-29T03:43:00Z", "2026-04-29T04:03:00Z"),
            ]:
                self._reconcile(conn, since, until, accuracy_threshold_m=100.0)

            visits = location_db.get_visits(conn)
            assert len(visits) == 1, (
                f"Expected 1 visit after sliding-window runs, got {len(visits)}: "
                f"{[(v.id, v.entered_at, v.exited_at) for v in visits]}"
            )
            assert visits[0].entered_at == "2026-04-29T03:38:00Z"
            assert visits[0].exited_at == "2026-04-29T03:49:30Z"
            assert visits[0].ping_count == 20

    def test_visit_straddling_since_not_truncated(self, tmp_path):
        """A visit whose first ping is before `since` must be reconstructed in full."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # Visit pings span 09:50 - 10:10; reconcile window starts at 10:00.
            for m in range(50, 60, 2):
                self._ping(conn, f"2026-04-21T09:{m:02d}:00Z", pid)
            for m in range(0, 12, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            self._reconcile(conn, "2026-04-21T10:00:00Z", "2026-04-21T11:00:00Z",
                            accuracy_threshold_m=100.0)

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-21T09:50:00Z", (
                "Read-back must find the visit's true first ping outside the window"
            )
            assert visits[0].exited_at == "2026-04-21T10:10:00Z"

    def test_visit_entirely_before_window_untouched(self, tmp_path):
        """A closed visit that ended before `since` must be left alone."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            # Pre-existing closed visit from earlier in the day
            old_id = location_db.open_visit(conn, pid, "home", "2026-04-21T08:00:00Z")
            location_db.close_visit(conn, old_id, "2026-04-21T08:30:00Z")
            # Pings for a different, later visit that we will reconcile
            for m in range(0, 12, 2):
                self._ping(conn, f"2026-04-21T10:{m:02d}:00Z", pid)
            conn.commit()

            self._reconcile(conn, "2026-04-21T09:00:00Z", "2026-04-21T11:00:00Z",
                            accuracy_threshold_m=100.0)

            visits = sorted(location_db.get_visits(conn), key=lambda v: v.entered_at)
            assert len(visits) == 2
            assert visits[0].id == old_id
            assert visits[0].entered_at == "2026-04-21T08:00:00Z"
            assert visits[1].entered_at == "2026-04-21T10:00:00Z"

    def test_reconcile_with_pings_linked_to_old_visit(self, tmp_path):
        """Pings with visit_id pointing at the about-to-be-deleted visit must
        not trigger FOREIGN KEY constraint failed. Regression for the per-user
        location.db split: `connect()` enables `PRAGMA foreign_keys = ON`,
        which the framework istota.db never did.
        """
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = self._home(conn)
            old_visit_id = location_db.open_visit(conn, pid, "home", "2026-04-21T10:00:00Z")
            location_db.close_visit(conn, old_visit_id, "2026-04-21T10:28:00Z")
            # Pings with visit_id set — the realistic state after live ingest
            for m in range(0, 30, 2):
                location_db.insert_ping(
                    conn, f"2026-04-21T10:{m:02d}:00Z", 0.0, 0.0,
                    accuracy=10.0, place_id=pid, visit_id=old_visit_id,
                )
            conn.commit()

            assert self._reconcile(conn, *_APR21) == 1
            assert len(location_db.get_visits(conn)) == 1

    def test_cleans_up_phantoms_from_prior_buggy_runs(self, tmp_path):
        """Pre-existing duplicate visits (from the bug) must be replaced by one."""
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            pid = location_db.add_place(conn, "lazy_acres", 32.78, -117.18)
            # Three phantom visits with staggered entries, identical exits, no pings linked
            for entry in ("38:00", "40:15", "43:30"):
                vid = location_db.open_visit(conn, pid, "lazy_acres", f"2026-04-29T03:{entry}Z")
                location_db.close_visit(conn, vid, "2026-04-29T03:49:30Z")
            # Real pings (would be linked to a different visit_id in production)
            for mmss in ("38:00", "39:00", "42:00", "45:00", "47:00", "49:30"):
                self._ping(conn, f"2026-04-29T03:{mmss}Z", pid)
            conn.commit()

            self._reconcile(conn, "2026-04-29T03:00:00Z", "2026-04-29T04:30:00Z",
                            accuracy_threshold_m=100.0)

            visits = location_db.get_visits(conn)
            assert len(visits) == 1
            assert visits[0].entered_at == "2026-04-29T03:38:00Z"
            assert visits[0].exited_at == "2026-04-29T03:49:30Z"


class TestCurrentLastAlias:
    """`last` is an alias for `current` — the natural name an LLM reaches for."""

    def test_last_alias_parses(self):
        from istota.skills.location import build_parser
        args = build_parser().parse_args(["last"])
        assert args.command == "last"

    def test_last_alias_dispatches_to_cmd_current(self):
        from istota.skills.location import main
        with patch("istota.skills.location.cmd_current") as m, \
                patch.object(sys, "argv", ["loc", "last"]):
            main()
        assert m.called


class TestGarminImportSkill:
    """The location skill's import-garmin-tracks subcommand. In a sandbox
    (no master key) it delegates by writing a deferred op the scheduler
    runs post-task."""

    def _run(self, args, env, monkeypatch):
        from istota import secrets_store
        from istota.skills.location import cmd_import_garmin_tracks

        # Force the delegated path deterministically.
        monkeypatch.setattr(secrets_store, "secret_key_available", lambda: False)
        captured = io.StringIO()
        with patch.dict("os.environ", env, clear=False), patch.object(sys, "stdout", captured):
            try:
                cmd_import_garmin_tracks(args)
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, captured.getvalue()

    def _task_env(self, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            "ISTOTA_USER_ID": "alice",
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "99",
        }
        return deferred, env

    def test_delegated_write(self, tmp_path, monkeypatch):
        deferred, env = self._task_env(tmp_path)
        code, out = self._run(MagicMock(days_back=14, dry_run=False), env, monkeypatch)
        assert code == 0
        payload = json.loads(out)
        assert payload["status"] == "ok" and payload["queued"] is True
        opfile = deferred / "task_99_garmin_import.json"
        assert opfile.exists()
        assert json.loads(opfile.read_text()) == {"days_back": 14}

    def test_delegated_dry_run_rejected(self, tmp_path, monkeypatch):
        deferred, env = self._task_env(tmp_path)
        code, out = self._run(MagicMock(days_back=7, dry_run=True), env, monkeypatch)
        assert code == 1
        assert "dry-run is only available in direct mode" in json.loads(out)["error"]
        assert not (deferred / "task_99_garmin_import.json").exists()

    def test_no_task_context_errors(self, tmp_path, monkeypatch):
        # No ISTOTA_DEFERRED_DIR / ISTOTA_TASK_ID → can't delegate.
        env = {"ISTOTA_USER_ID": "alice",
               "ISTOTA_DEFERRED_DIR": "", "ISTOTA_TASK_ID": ""}
        code, out = self._run(MagicMock(days_back=7, dry_run=False), env, monkeypatch)
        assert code == 1
        assert "web UI" in json.loads(out)["error"]


# ---------------------------------------------------------------------------
# ISSUE-558 — imported watch activities in history and day-summary
# ---------------------------------------------------------------------------

_HOME = (34.0500, -118.2500)
_FRIENDS = (34.0300, -118.3000)


def _run_from_home_pings(start="2026-03-08T21:34:20Z", count=148):
    """A watch-recorded loop that leaves home and comes back, every 10 s.

    Out west along one street, back along a parallel one ~170 m north, so
    the track starts and ends within a few tens of metres of the door.
    """
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    half = count // 2
    pings = []
    for i in range(count):
        if i < half:
            lat, lon = _HOME[0] + 0.0003, _HOME[1] - 0.0005 - 0.013 * i / (half - 1)
        else:
            j = i - half
            lat = _HOME[0] + 0.0003 + 0.0015 * min(1.0, j / 4)
            lon = _HOME[1] - 0.0135 + 0.013 * j / (count - half - 1)
            if j >= count - half - 4:
                lat = _HOME[0] + 0.0002
        ts = t0 + timedelta(seconds=10 * i)
        pings.append({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": lat, "lon": lon, "place_id": None,
            "activity_type": "running", "source": "garmin", "accuracy": None,
        })
    return pings


def _run_day_pings(*, interleave_wifi_zone=False):
    """The 2026-09-27 day from ISSUE-558, moved to a fixture date.

    Home from 12:05 to 14:13 local, a phone that goes quiet while it sits
    there, a watch-only run 14:34-14:58, the phone silent again until a
    wifi-zone home ping at 16:23 and a drive to a friend's at 17:16.
    """
    home = [
        {"timestamp": f"2026-03-08T{19 + (5 + 4 * i) // 60:02d}:{(5 + 4 * i) % 60:02d}:00Z",
         "lat": _HOME[0], "lon": _HOME[1], "place_id": 1}
        for i in range(33)  # 19:05 .. 21:13 UTC
    ]
    run = _run_from_home_pings()
    wifi = []
    if interleave_wifi_zone:
        # A phone left on the home network keeps declaring home through the run.
        wifi = [
            {"timestamp": f"2026-03-08T21:{m:02d}:05Z", "lat": _HOME[0], "lon": _HOME[1],
             "place_id": 1, "wifi_zone": True, "activity_type": "stationary"}
            for m in range(35, 58, 5)
        ]
    leave = [
        {"timestamp": "2026-03-08T23:23:41Z", "lat": _HOME[0], "lon": _HOME[1],
         "place_id": 1, "wifi_zone": True, "activity_type": "driving"},
        *[
            {"timestamp": f"2026-03-08T23:{24 + i:02d}:00Z",
             "lat": _HOME[0] - 0.0009, "lon": _HOME[1] - 0.002 - 0.004 * i,
             "speed": 8.0, "activity_type": "driving"}
            for i in range(20)
        ],
    ]
    friends = [
        {"timestamp": f"2026-03-09T00:{16 + 4 * i:02d}:00Z",
         "lat": _FRIENDS[0], "lon": _FRIENDS[1], "place_id": 2}
        for i in range(10)
    ]
    return sorted(home + run + wifi + leave + friends, key=lambda p: p["timestamp"])


_RUN_DAY_PLACES = [
    {"name": "Home", "lat": _HOME[0], "lon": _HOME[1], "radius_meters": 100},
    {"name": "Friends", "lat": _FRIENDS[0], "lon": _FRIENDS[1], "radius_meters": 100},
]

_HOME_AROUND_RUN = [("Home", "12:05", "14:34"), ("Home", "14:58", "16:23")]


def _spans(result):
    return [(s["location"], s["arrived"], s["departed"]) for s in result["stops"]]


def _sorted_pings(*groups):
    return sorted([p for group in groups for p in group], key=lambda p: p["timestamp"])


def _without_garmin(pings):
    return [p for p in pings if p.get("source") != "garmin"]


class TestDaySummaryActivities:
    """A tracked activity that leaves a place and returns is its own segment."""

    def _summary(self, tmp_path, pings=None, places=_RUN_DAY_PLACES, **kw):
        return _run_day_summary(
            tmp_path, pings=pings if pings is not None else _run_day_pings(**kw), places=places,
        )

    def test_the_run_is_reported_as_an_activity(self, tmp_path):
        result = self._summary(tmp_path)

        assert len(result["activities"]) == 1
        run = result["activities"][0]
        assert run["type"] == "activity"
        assert run["activity"] == "running"
        assert run["source"] == "garmin"
        assert (run["start"], run["end"]) == ("14:34", "14:58")
        assert run["duration_minutes"] == 24
        assert run["ping_count"] == 148
        assert 2.0 < run["distance_km"] < 3.5
        assert run["start_place"] == "Home"
        assert run["end_place"] == "Home"

    def test_the_home_stop_is_split_around_the_run(self, tmp_path):
        result = self._summary(tmp_path)

        assert _spans(result) == [*_HOME_AROUND_RUN, ("Friends", "17:16", "17:52")]
        # The run's pings do not count toward the stops either side of it.
        assert result["stops"][0]["ping_count"] == 33
        assert result["stops"][1]["ping_count"] == 1

    def test_wifi_zone_pings_during_the_run_do_not_bridge_the_stop(self, tmp_path):
        """A phone left home declares home all through the run; the watch says otherwise."""
        result = self._summary(tmp_path, interleave_wifi_zone=True)

        assert _spans(result)[:2] == _HOME_AROUND_RUN
        assert len(result["activities"]) == 1

    def test_backfilled_watch_pings_near_the_door_still_end_the_stop(self, tmp_path):
        """Backfilling a place tags the run's first and last points with it."""
        pings = _run_day_pings()
        for p in pings:
            if p.get("source") == "garmin" and haversine(p["lat"], p["lon"], *_HOME) <= 100:
                p["place_id"] = 1
        assert any(p.get("source") == "garmin" and p["place_id"] == 1 for p in pings)

        assert _spans(self._summary(tmp_path, pings))[:2] == _HOME_AROUND_RUN

    def test_a_late_return_is_not_bridged_back_to_the_run(self, tmp_path):
        """A run ending home at 06:34, and the phone next heard at home ten hours later."""
        pings = [p for p in _run_day_pings() if not (
            p.get("source") == "garmin" or p["timestamp"] < "2026-03-08T21:30:00Z"
        )]
        result = self._summary(
            tmp_path, _sorted_pings(pings, _run_from_home_pings(start="2026-03-08T13:10:00Z")),
        )

        assert result["activities"][0]["end"] == "06:34"
        assert "06:34" not in [s["arrived"] for s in result["stops"]]

    def test_a_stray_watch_fix_does_not_split_a_stop(self, tmp_path):
        stray = {"timestamp": "2026-03-08T20:30:05Z", "lat": _HOME[0] + 0.0001,
                 "lon": _HOME[1], "place_id": None, "activity_type": "running",
                 "source": "garmin", "accuracy": None}
        base = _without_garmin(_run_day_pings())

        without = self._summary(tmp_path / "a", base)
        with_stray = self._summary(tmp_path / "b", _sorted_pings(base, [stray]))

        assert with_stray["activities"] == []
        assert _spans(with_stray) == _spans(without)

    def test_the_run_ends_at_the_nearest_place_not_the_first_listed(self, tmp_path):
        """Ingest tags a ping with the nearest place; the reopen has to agree with it."""
        gym = {"name": "Gym", "lat": _HOME[0] + 0.0009, "lon": _HOME[1] - 0.0010,
               "radius_meters": 150}
        places = [gym, *_RUN_DAY_PLACES]  # Gym is id 1, Home 2, Friends 3
        pings = _run_day_pings()
        for p in pings:
            if p.get("place_id"):
                p["place_id"] += 1

        result = self._summary(tmp_path, pings, places)

        assert result["activities"][0]["end_place"] == "Home"
        assert _spans(result)[:2] == _HOME_AROUND_RUN

    def test_a_start_just_outside_a_tight_radius_takes_the_stop_it_leaves(self, tmp_path):
        """The run's first point is ~57 m from the door and its last ~51 m (#558 follow-up)."""
        places = [{**_RUN_DAY_PLACES[0], "radius_meters": 54}, _RUN_DAY_PLACES[1]]
        run = [p for p in _run_day_pings() if p.get("source") == "garmin"]
        assert haversine(run[0]["lat"], run[0]["lon"], *_HOME) > 54
        assert haversine(run[-1]["lat"], run[-1]["lon"], *_HOME) <= 54

        activity = self._summary(tmp_path, places=places)["activities"][0]
        assert (activity["start_place"], activity["end_place"]) == ("Home", "Home")

    def test_a_start_far_from_the_stop_before_it_has_no_place(self, tmp_path):
        """A run from a trailhead a kilometre off is not named for the stop before it."""
        pings = _run_day_pings()
        for p in pings:
            if p.get("source") == "garmin":
                p["lon"] -= 0.012

        result = self._summary(tmp_path, pings)

        assert result["stops"][0]["location"] == "Home"
        activity = result["activities"][0]
        assert (activity["start_place"], activity["end_place"]) == (None, None)

    def test_a_start_takes_no_name_from_across_another_activity(self, tmp_path):
        """A dropout splits a run in two; the second half did not leave from home."""
        t0 = datetime.fromisoformat("2026-03-08T21:34:20+00:00")

        def leg(start, lons, lat_off):
            return [{
                "timestamp": (start + timedelta(seconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lat": _HOME[0] + lat_off, "lon": _HOME[1] + lon, "place_id": None,
                "activity_type": "running", "source": "garmin", "accuracy": None,
            } for i, lon in enumerate(lons)]

        out = [-0.0005 - 0.004 * i / 9 for i in range(10)]
        back = [-0.0045 + 0.003 * i / 9 for i in range(10)]
        first = leg(t0, out + back, 0.0003)           # ends ~140 m from the door
        second = leg(t0 + timedelta(seconds=190 + 720),
                     [-0.0015 + 0.0010 * i / 19 for i in range(20)], 0.0003)
        places = [{**_RUN_DAY_PLACES[0], "radius_meters": 54}, _RUN_DAY_PLACES[1]]
        assert 54 < haversine(second[0]["lat"], second[0]["lon"], *_HOME) <= 250

        result = self._summary(
            tmp_path, _sorted_pings(_without_garmin(_run_day_pings()), first, second), places,
        )

        a, b = result["activities"]
        assert a["start_place"] == "Home"
        assert (a["end_place"], b["start_place"]) == (None, None)

    def test_an_end_just_outside_the_radius_is_not_named(self, tmp_path):
        """No stop resumes there, so naming the place would leave its gap unexplained."""
        places = [{**_RUN_DAY_PLACES[0], "radius_meters": 40}, _RUN_DAY_PLACES[1]]
        run = [p for p in _run_day_pings() if p.get("source") == "garmin"]
        assert 40 < haversine(run[-1]["lat"], run[-1]["lon"], *_HOME) <= 250
        # The phone is next heard at home an hour on: a Home stop to borrow from.
        later = [
            {"timestamp": f"2026-03-08T22:{m:02d}:00Z", "lat": _HOME[0], "lon": _HOME[1],
             "place_id": 1}
            for m in range(0, 21, 4)
        ]

        result = self._summary(tmp_path, _sorted_pings(_run_day_pings(), later), places)

        activity = result["activities"][0]
        assert (activity["start_place"], activity["end_place"]) == ("Home", None)
        assert "14:58" not in [s["arrived"] for s in result["stops"]]
        assert result["stops"][1]["location"] == "Home"

    def test_a_start_takes_no_name_from_a_stop_that_is_not_a_saved_place(self, tmp_path):
        """A geocoded neighbourhood is not a place an activity can start at."""
        pings = _run_day_pings()
        for p in pings:
            p["place_id"] = 1 if p.get("place_id") == 2 else None

        result = self._summary(tmp_path, pings, [_RUN_DAY_PLACES[1]])

        assert result["stops"][0]["location_source"] not in ("saved_place", "saved_place_proximity")
        assert result["activities"][0]["start_place"] is None

    def test_a_day_without_imported_tracks_has_no_activities(self, tmp_path):
        result = self._summary(tmp_path, _without_garmin(_run_day_pings()))
        assert result["activities"] == []


class TestHistoryCarriesSource:
    def _seed(self, tmp_path):
        db_path = _init_loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            location_db.insert_ping(conn, "2026-03-16T20:00:00Z", 34.0, -118.0,
                                    activity_type="running", source="garmin")
            location_db.insert_ping(conn, "2026-03-16T20:01:00Z", 34.0, -118.0,
                                    accuracy=5.0, activity_type="running")
            conn.commit()
        return db_path

    def test_each_ping_names_its_source(self, tmp_path):
        with _loc_env(self._seed(tmp_path)):
            output = _run_cmd(_skill().cmd_history, limit=10, date=None)

        assert {p["timestamp"]: p["source"] for p in output} == {
            "2026-03-16T20:00:00Z": "garmin",
            "2026-03-16T20:01:00Z": "overland",
        }

    def test_source_filters_the_history(self, tmp_path):
        with _loc_env(self._seed(tmp_path)):
            dated = _run_cmd(_skill().cmd_history, limit=0, date="2026-03-16",
                             tz="America/Los_Angeles", source="garmin")
            undated = _run_cmd(_skill().cmd_history, limit=10, date=None, source="overland")

        assert [p["source"] for p in dated] == ["garmin"]
        assert [p["source"] for p in undated] == ["overland"]
