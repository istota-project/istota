"""Tests for location tracking: loader, DB functions, haversine, state machine, CLI."""

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.geo import haversine
from istota.location_loader import (
    LocationAction,
    LocationConfig,
    LocationPlace,
    LocationSettings,
    build_token_user_map,
    load_location_config,
    parse_location_data,
    sync_places_to_db,
)
from istota.webhook_receiver import resolve_place
from istota.storage import get_user_location_path


@pytest.fixture
def mount_path(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    return mount


@pytest.fixture
def make_config(tmp_path, mount_path):
    def _make(**overrides):
        db_path = overrides.pop("db_path", tmp_path / "test.db")
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            temp_dir=tmp_path / "temp",
            **overrides,
        )
    return _make


def _write_location_md(mount_path, user_id, content, bot_dir="istota"):
    loc_path = mount_path / get_user_location_path(user_id, bot_dir).lstrip("/")
    loc_path.parent.mkdir(parents=True, exist_ok=True)
    loc_path.write_text(content)


def _init_db(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


# ===========================================================================
# Location loader tests
# ===========================================================================


class TestParseLocationData:
    def test_empty_data(self):
        cfg = parse_location_data({})
        assert cfg.settings.ingest_token == ""
        assert cfg.settings.default_radius == 25
        assert cfg.places == []
        assert cfg.actions == []

    def test_settings(self):
        cfg = parse_location_data({
            "settings": {"ingest_token": "secret123", "default_radius": 200},
        })
        assert cfg.settings.ingest_token == "secret123"
        assert cfg.settings.default_radius == 200

    def test_places(self):
        cfg = parse_location_data({
            "places": [
                {"name": "home", "lat": 34.0, "lon": -118.0, "radius_meters": 150, "category": "home"},
                {"name": "gym", "lat": 34.1, "lon": -118.1},
            ],
        })
        assert len(cfg.places) == 2
        assert cfg.places[0].name == "home"
        assert cfg.places[0].lat == 34.0
        assert cfg.places[0].radius_meters == 150
        assert cfg.places[0].category == "home"
        assert cfg.places[1].name == "gym"
        assert cfg.places[1].radius_meters == 25  # default

    def test_places_use_default_radius(self):
        cfg = parse_location_data({
            "settings": {"default_radius": 200},
            "places": [{"name": "x", "lat": 0, "lon": 0}],
        })
        assert cfg.places[0].radius_meters == 200

    def test_places_skip_unnamed(self):
        cfg = parse_location_data({
            "places": [{"name": "", "lat": 0, "lon": 0}, {"lat": 1, "lon": 1}],
        })
        assert len(cfg.places) == 0

    def test_actions(self):
        cfg = parse_location_data({
            "actions": [
                {
                    "trigger": "enter", "place": "gym",
                    "message": "Arrived", "surface": "ntfy", "priority": "high",
                },
                {
                    "trigger": "exit", "place": "home",
                    "surface": "silent",
                },
                {
                    "trigger": "dwell", "place": "airport",
                    "surface": "cron_prompt", "prompt": "check flights",
                    "dwell_minutes": 120,
                },
            ],
        })
        assert len(cfg.actions) == 3
        assert cfg.actions[0].trigger == "enter"
        assert cfg.actions[0].place == "gym"
        assert cfg.actions[0].message == "Arrived"
        assert cfg.actions[0].priority == "high"
        assert cfg.actions[1].surface == "silent"
        assert cfg.actions[2].surface == "cron_prompt"
        assert cfg.actions[2].dwell_minutes == 120

    def test_actions_skip_incomplete(self):
        cfg = parse_location_data({
            "actions": [
                {"trigger": "", "place": "gym"},
                {"trigger": "enter", "place": ""},
                {"trigger": "enter"},
            ],
        })
        assert len(cfg.actions) == 0


class TestLoadLocationConfig:
    def test_load_valid_file(self, mount_path, make_config):
        config = make_config()
        _write_location_md(mount_path, "alice", """\
# Location Tracking

```toml
[settings]
ingest_token = "tok123"
default_radius = 200

[[places]]
name = "home"
lat = 34.05
lon = -118.4
radius_meters = 150
category = "home"

[[actions]]
trigger = "enter"
place = "home"
message = "Welcome home"
surface = "ntfy"
```
""")
        cfg = load_location_config(config, "alice")
        assert cfg is not None
        assert cfg.settings.ingest_token == "tok123"
        assert len(cfg.places) == 1
        assert cfg.places[0].name == "home"
        assert len(cfg.actions) == 1

    def test_no_file_returns_none(self, make_config):
        config = make_config()
        assert load_location_config(config, "alice") is None

    def test_no_toml_block_returns_empty(self, mount_path, make_config):
        config = make_config()
        _write_location_md(mount_path, "alice", "# Location\n\nJust text, no TOML.")
        cfg = load_location_config(config, "alice")
        assert cfg is not None
        assert cfg.places == []

    def test_no_mount_returns_none(self, tmp_path):
        config = Config(nextcloud_mount_path=None)
        assert load_location_config(config, "alice") is None


class TestSyncPlacesToDb:
    def test_inserts_and_deletes(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            # Insert initial places
            db.insert_place(conn, "alice", "old", 1.0, 2.0)
            conn.commit()

            # Sync with new list
            new_places = [
                LocationPlace("home", 34.0, -118.0, 150, "home"),
                LocationPlace("gym", 34.1, -118.1, 75, "gym"),
            ]
            sync_places_to_db(conn, "alice", new_places)

            places = db.get_places(conn, "alice")
            names = {p.name for p in places}
            assert "home" in names
            assert "gym" in names
            assert "old" not in names

    def test_updates_existing(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            db.insert_place(conn, "alice", "home", 1.0, 2.0, radius_meters=50)
            conn.commit()

            sync_places_to_db(conn, "alice", [
                LocationPlace("home", 34.0, -118.0, 200, "home"),
            ])

            place = db.get_place_by_name(conn, "alice", "home")
            assert place.lat == 34.0
            assert place.radius_meters == 200


class TestBuildTokenUserMap:
    def test_builds_map(self, mount_path, make_config):
        config = make_config(users={"alice": UserConfig(), "bob": UserConfig()})
        _write_location_md(mount_path, "alice", """
```toml
[settings]
ingest_token = "tok-alice"
```
""")
        _write_location_md(mount_path, "bob", """
```toml
[settings]
ingest_token = "tok-bob"
```
""")
        token_map = build_token_user_map(config)
        assert token_map == {"tok-alice": "alice", "tok-bob": "bob"}

    def test_skips_users_without_token(self, mount_path, make_config):
        config = make_config(users={"alice": UserConfig()})
        _write_location_md(mount_path, "alice", "# Location\n\n```toml\n```\n")
        token_map = build_token_user_map(config)
        assert token_map == {}


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


class TestStateMachine:
    """Tests for the state machine logic in webhook_receiver."""

    def _process(self, conn, user_id, place_id, place, timestamp, actions=None):
        from istota.webhook_receiver import _update_state_machine
        ping_id = db.insert_location_ping(
            conn, user_id, timestamp, 0.0, 0.0,
        )
        _update_state_machine(
            conn, user_id, ping_id, place_id, place,
            timestamp, actions or [],
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

    def test_actions_fire_on_transition(self, tmp_path):
        db_path = _init_db(tmp_path)
        with db.get_db(db_path) as conn:
            pid_home = db.insert_place(conn, "alice", "home", 34.0, -118.0)
            pid_gym = db.insert_place(conn, "alice", "gym", 34.1, -118.1)
            home = db.get_place_by_name(conn, "alice", "home")
            gym = db.get_place_by_name(conn, "alice", "gym")

            actions = [
                LocationAction(trigger="exit", place="home", message="Left home", surface="silent"),
                LocationAction(trigger="enter", place="gym", message="At gym", surface="silent"),
            ]

            # Establish at home
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:00:00Z")
            self._process(conn, "alice", pid_home, home, "2026-02-20T10:05:00Z")

            # Transition to gym (silent actions, just verifying no errors)
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:10:00Z", actions)
            self._process(conn, "alice", pid_gym, gym, "2026-02-20T10:15:00Z", actions)

            # If we got here without errors, silent actions worked
            state = db.get_location_state(conn, "alice")
            assert state.current_place_id == pid_gym


# ===========================================================================
# Overland payload parsing tests
# ===========================================================================


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
            _process_feature(conn, "alice", feature, [], [])
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
            _process_feature(conn, "alice", feature, [], [])
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
            _process_feature(conn, "alice", feature, [], [])
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
            _process_feature(conn, "alice", feature, [], [])
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
