"""ISSUE-096 — follow the user's timezone when they travel.

The detection half: turn a run of pings into "you have been in zone Z long
enough that Z is now your timezone", or into nothing.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

try:
    import timezonefinder  # noqa: F401
    _has_tzf = True
except ImportError:
    _has_tzf = False

_needs_tzf = pytest.mark.skipif(not _has_tzf, reason="timezonefinder not installed")

from istota.location import db as location_db
from istota.location import timezone as loc_tz


LAX = (33.94, -118.40)
WARSAW = (52.16, 20.97)
MID_PACIFIC = (10.0, -160.0)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _loc_db(tmp_path):
    db_path = tmp_path / "location.db"
    location_db.init_db(db_path)
    return db_path


def _add(conn, minutes_ago: float, latlon, accuracy: float | None = 8.0, *, tz=None):
    """Insert a ping `minutes_ago` before NOW.

    ``tz`` stamps the timestamp in that zone's local time with an explicit
    offset, which is what a real device sends — the webhook stores the client's
    string verbatim.
    """
    at = NOW - timedelta(minutes=minutes_ago)
    ts = at.astimezone(tz).isoformat() if tz else at.strftime("%Y-%m-%dT%H:%M:%SZ")
    location_db.insert_ping(
        conn, ts, latlon[0], latlon[1], accuracy=accuracy, activity_type="stationary",
    )


@_needs_tzf
class TestTimezoneAt:
    def test_resolves_a_known_city(self):
        assert loc_tz.timezone_at(*WARSAW) == "Europe/Warsaw"

    def test_refuses_an_open_ocean_zone(self):
        """Mid-ocean resolves to an `Etc/GMT±N` offset, not a place.

        That is what a phone reports halfway through a long flight, and adopting
        it would set the user's timezone to a fixed offset they are only passing
        through.
        """
        assert loc_tz.timezone_at(*MID_PACIFIC) is None

    def test_returns_none_without_the_dependency(self, monkeypatch):
        monkeypatch.setattr(loc_tz, "_finder", lambda: None)
        assert loc_tz.timezone_at(*WARSAW) is None


@_needs_tzf
class TestDetectTravelTimezone:
    def test_detects_a_sustained_move(self, tmp_path):
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 120, 90, 30, 2):
                _add(conn, minutes, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found == "Europe/Warsaw"

    def test_ignores_a_zone_the_user_is_already_in(self, tmp_path):
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, LAX)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_will_not_fire_on_a_fresh_arrival(self, tmp_path):
        """Landing is not staying. Nothing older than the dwell window is in the
        new zone yet, so the move is not sustained."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _add(conn, 180, LAX)
            _add(conn, 120, LAX)
            _add(conn, 20, WARSAW)
            _add(conn, 2, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_will_not_fire_while_crossing_zones(self, tmp_path):
        """Mid-flight the trailing ping is over water; the two ends disagree."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _add(conn, 120, LAX)
            _add(conn, 90, MID_PACIFIC)
            _add(conn, 2, MID_PACIFIC)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_ignores_a_stale_track(self, tmp_path):
        """The tracker has been off for a day. Yesterday's position is not
        evidence about where the user is now."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            _add(conn, 60 * 30, WARSAW)
            _add(conn, 60 * 26, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_ignores_low_accuracy_pings(self, tmp_path):
        """A junk fix can land hundreds of kilometres away, which across a
        border is a different zone entirely."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 120, 90, 30, 2):
                _add(conn, minutes, LAX)
            _add(conn, 1, WARSAW, accuracy=5000.0)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_no_pings_at_all(self, tmp_path):
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_an_unset_current_zone_still_resolves(self, tmp_path):
        """A user on the install default should get a real zone, not be treated
        as already correct."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(conn, "UTC", dwell_minutes=60, now=NOW)

        assert found == "Europe/Warsaw"

    def test_reads_timestamps_carrying_an_offset(self, tmp_path):
        """The webhook stores the client's string verbatim, so the column holds
        `+02:00` and `-0700` forms as well as `Z`.

        Compared as text, a `+02:00` stamp sorts above a UTC `now`, so a naive
        `WHERE timestamp <= ?` bound hides every recent row and the feature
        silently never fires for such a device.
        """
        db_path = _loc_db(tmp_path)
        warsaw_local = ZoneInfo("Europe/Warsaw")
        with location_db.connect(db_path) as conn:
            for minutes in (180, 120, 90, 30, 2):
                _add(conn, minutes, WARSAW, tz=warsaw_local)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found == "Europe/Warsaw"

    def test_reads_a_timestamp_with_microseconds_and_no_zone(self, tmp_path):
        """The webhook's own fallback stamps `datetime.now(utc).isoformat()`."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                at = NOW - timedelta(minutes=minutes)
                location_db.insert_ping(
                    conn, at.replace(tzinfo=None).isoformat(timespec="microseconds"),
                    WARSAW[0], WARSAW[1], accuracy=8.0, activity_type="stationary",
                )
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found == "Europe/Warsaw"

    def test_will_not_fire_mid_flight_over_land(self, tmp_path):
        """A continental flight spends hours over one zone without landing in it.

        The `Etc/` guard only covers open water, so the dwell window alone would
        set the timezone to whatever is being flown over — and then again on
        arrival. Staying put, not elapsed time, is what makes a zone yours.
        """
        db_path = _loc_db(tmp_path)
        # LAX -> JFK, still airborne: two hours of pings inside America/Chicago,
        # ~10 minutes apart, moving the whole way.
        leg = [
            (150, (36.35, -102.34)),
            (120, (37.20, -97.80)),
            (90, (37.90, -93.10)),
            (60, (38.10, -90.60)),
            (30, (38.36, -88.95)),
            (2, (38.60, -86.20)),
        ]
        with location_db.connect(db_path) as conn:
            for minutes, latlon in leg:
                _add(conn, minutes, latlon)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_settles_once_the_flight_lands(self, tmp_path):
        db_path = _loc_db(tmp_path)
        jfk = (40.64, -73.78)
        with location_db.connect(db_path) as conn:
            _add(conn, 300, (36.35, -102.34))
            for minutes in (90, 60, 30, 2):
                _add(conn, minutes, jfk)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found == "America/New_York"

    def test_a_renamed_zone_is_not_a_move(self, tmp_path):
        """`US/Pacific` is a tzdata link to `America/Los_Angeles`, and Nextcloud
        seeds several such names. Comparing strings alone tells a user who has
        not moved that they have."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, LAX)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "US/Pacific", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_ignores_pings_with_no_accuracy_at_all(self, tmp_path):
        """An imported Garmin track carries no accuracy. Passing those through
        would let a backfilled activity anchor the decision."""
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, WARSAW, accuracy=None)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60, now=NOW,
            )

        assert found is None

    def test_a_naive_now_is_read_as_utc(self, tmp_path):
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60,
                now=NOW.replace(tzinfo=None),
            )

        assert found == "Europe/Warsaw"

    def test_a_non_utc_now_is_converted(self, tmp_path):
        db_path = _loc_db(tmp_path)
        with location_db.connect(db_path) as conn:
            for minutes in (180, 90, 2):
                _add(conn, minutes, WARSAW)
            conn.commit()

            found = loc_tz.detect_travel_timezone(
                conn, "America/Los_Angeles", dwell_minutes=60,
                now=NOW.astimezone(ZoneInfo("America/Los_Angeles")),
            )

        assert found == "Europe/Warsaw"


@_needs_tzf
class TestSameWallClock:
    def test_a_link_and_its_target_agree(self):
        assert loc_tz.same_wall_clock("US/Pacific", "America/Los_Angeles", NOW)

    def test_distinct_zones_disagree(self):
        assert not loc_tz.same_wall_clock("Europe/Warsaw", "America/Los_Angeles", NOW)

    def test_an_unknown_name_compares_unequal_rather_than_raising(self):
        assert not loc_tz.same_wall_clock("Not/AZone", "America/Los_Angeles", NOW)
