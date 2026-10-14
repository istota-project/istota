"""The observation projection shared by one-time and granted room reads."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from istota.location import db as location_db, logic


NOW = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)
UNAVAILABLE = {"error": "no_fresh_location"}
FIELDS = {
    "precision", "observed_at", "age_seconds", "accuracy_m",
    "observation_kind", "freshness", "lat", "lon", "map_url",
}


@pytest.fixture
def location_path(tmp_path):
    path = tmp_path / "location.db"
    location_db.init_db(path)
    return path


def add_ping(path, timestamp="2026-03-08T11:59:00Z", **overrides):
    values = {"lat": 12.345678, "lon": -45.678912, "accuracy": 10}
    values.update(overrides)
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, timestamp, **values)
        conn.commit()


def snapshot(path, **overrides):
    options = {"now": NOW, "precision": "exact", "accuracy_threshold_m": 100}
    options.update(overrides)
    return logic.location_snapshot(path, **options)


def test_snapshot_projects_phone_observation_without_personal_metadata(location_path):
    with location_db.connect(location_path) as conn:
        place_id = conn.execute(
            "INSERT INTO places(name, lat, lon, notes) VALUES (?, ?, ?, ?)",
            ("Private place", 12.345678, -45.678912, "Private note"),
        ).lastrowid
        visit_id = conn.execute(
            "INSERT INTO visits(place_id, place_name, entered_at, ping_count) "
            "VALUES (?, ?, ?, ?)",
            (place_id, "Private place", "2026-03-08T10:00:00Z", 50),
        ).lastrowid
        conn.commit()
    add_ping(location_path, altitude=24, speed=3, course=90, battery=0.5,
             activity_type="walking", wifi="Private wifi", client_id="device-point",
             place_id=place_id, visit_id=visit_id)
    # A later import must not displace the phone, even with a newer instant.
    add_ping(location_path, "2026-03-08T12:00:00Z", source="garmin", lat=20)

    result = snapshot(location_path)

    assert set(result) == FIELDS
    assert result == {
        "precision": "exact", "observed_at": "2026-03-08T11:59:00+00:00",
        "age_seconds": 60, "accuracy_m": 10, "observation_kind": "measured",
        "freshness": "fresh", "lat": 12.345678, "lon": -45.678912,
        "map_url": "https://www.openstreetmap.org/?mlat=12.345678&mlon=-45.678912#map=16/12.345678/-45.678912",
    }
    # Personal callers still get their original dashboard result.
    personal = logic.location_current(location_path)
    assert personal["last_ping"]["lat"] == 20
    assert personal["current_visit"]["place_name"] == "Private place"


def test_selection_orders_instants_and_breaks_equal_instants_by_id(location_path):
    add_ping(location_path, "2026-03-08T13:55:00+02:00", lat=10)
    add_ping(location_path, "2026-03-08T04:59:00-07:00", lat=11)
    add_ping(location_path, "2026-03-08T11:59:00", lat=12)
    # More than the timezone detector's bounded insertion-order scan.
    with location_db.connect(location_path) as conn:
        for _ in range(2001):
            location_db.insert_ping(conn, "2026-03-07T23:00:00Z", 13, 14, accuracy=10)
        conn.commit()

    result = snapshot(location_path)

    assert result["lat"] == 12
    assert result["observed_at"] == "2026-03-08T11:59:00+00:00"
    assert result["age_seconds"] == 60


@pytest.mark.parametrize(("age", "last_known", "freshness"), [
    (900, False, "fresh"), (900.001, False, None),
    (900.001, True, "last_known"), (86400, True, "last_known"),
    (86400.001, True, None), (-120, False, "fresh_clock_skew"),
    (-120.001, False, None),
])
def test_freshness_boundaries(location_path, age, last_known, freshness):
    add_ping(location_path, (NOW - timedelta(seconds=age)).isoformat())
    result = snapshot(location_path, allow_last_known=last_known)
    if freshness is None:
        assert result == UNAVAILABLE
    else:
        assert result["freshness"] == freshness
        assert result["age_seconds"] == pytest.approx(max(0, age))


@pytest.mark.parametrize("accuracy", [None, -1, 100.01, float("inf"), float("nan"), "bad"])
def test_bad_new_accuracy_never_falls_back_to_older_good_fix(location_path, accuracy):
    add_ping(location_path, "2026-03-08T11:58:00Z")
    add_ping(location_path, accuracy=accuracy)
    assert snapshot(location_path) == UNAVAILABLE
    assert snapshot(location_path, allow_last_known=True) == UNAVAILABLE


def test_accuracy_uses_the_callers_threshold(location_path):
    add_ping(location_path, accuracy=25)
    assert snapshot(location_path, accuracy_threshold_m=25)["accuracy_m"] == 25
    assert snapshot(location_path, accuracy_threshold_m=24) == UNAVAILABLE


@pytest.mark.parametrize("invalid", [
    {"timestamp": "not-a-date"}, {"timestamp": "2026-02-30T12:00:00Z"},
    {"timestamp": "2026-03-08T12:02:01Z"},
    {"lat": 91}, {"lon": -181}, {"lat": float("inf")}, {"lon": "bad"},
])
def test_invalid_observation_is_unusable(location_path, invalid):
    add_ping(location_path, **invalid)
    assert snapshot(location_path) == UNAVAILABLE
    add_ping(location_path, "2026-03-08T11:58:00Z", lat=15)
    assert snapshot(location_path)["lat"] == 15


def test_declared_area_point_has_no_exact_coordinate_or_accuracy_path(location_path):
    add_ping(location_path, wifi_zone=True, wifi="Private wifi", accuracy=1)
    result = snapshot(location_path, precision="area")
    assert set(result) == FIELDS
    assert result["observation_kind"] == "declared"
    assert result["precision"] == "area"
    assert result["accuracy_m"] is None
    assert (result["lat"], result["lon"]) == (12.35, -45.68)
    assert result["map_url"] == "https://www.openstreetmap.org/#map=12/12.35/-45.68"
    encoded = json.dumps(result)
    assert "12.345678" not in encoded
    assert "45.678912" not in encoded
    assert "Private wifi" not in encoded


def test_empty_and_import_only_databases_have_no_snapshot(location_path):
    assert snapshot(location_path) == UNAVAILABLE
    add_ping(location_path, source="garmin")
    assert snapshot(location_path, allow_last_known=True) == UNAVAILABLE


def test_invalid_precision_is_rejected_before_database_open(tmp_path):
    with pytest.raises(ValueError, match="precision"):
        snapshot(tmp_path / "absent" / "location.db", precision="street")


@pytest.mark.parametrize("accuracy", [10, 101])
def test_fractional_instants_tied_by_sqlite_still_select_newest(location_path, accuracy):
    # julianday rounds both instants to the same millisecond. The later
    # insertion is older, so ID alone must not decide which fix is current.
    add_ping(location_path, "2026-03-08T11:59:00.0004Z", lat=15, accuracy=accuracy)
    add_ping(location_path, "2026-03-08T11:59:00.0001Z", lat=16)
    result = snapshot(location_path)
    if accuracy > 100:
        assert result == UNAVAILABLE
    else:
        assert result["lat"] == 15
        assert result["observed_at"] == "2026-03-08T11:59:00.000400+00:00"
