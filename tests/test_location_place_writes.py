"""Naming a place after the fact, and the geofence bookkeeping behind it.

``place_id`` is resolved at ingest, so a place saved once the phone has
moved on leaves every historical ping inside it at NULL and
``location_day_summary`` goes on reporting the stop as an unnamed
coordinate. The web route had always repaired that on create; the skill
had no way even to name a coordinate it was not standing on (ISSUE-491).

Three properties are pinned here:

* ``learn`` sites a place from the coordinates it is given, and says
  where those coordinates came from — the complaint in the issue was
  not that the wrong place was saved but that nothing in the output
  said the coordinates were the phone's rather than the stop's.
* One helper, ``location_logic.assign_pings_to_place``, does the
  geofence bookkeeping for both surfaces and both verbs. The parity
  assertions below are what stop the two copies drifting again; the
  grep-shaped guard in ``test_location_surface_parity.py`` is what stops
  either surface growing its own back.
* The bookkeeping has two halves and both are needed on an update: a
  ping the place no longer contains is released, and an unattached ping
  it now contains is adopted.
"""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

try:
    import fastapi  # noqa: F401
    _has_fastapi = True
except ImportError:
    _has_fastapi = False

from istota.location import db as location_db

# Far from every other fixture in the suite, so a stray ping cannot be
# mistaken for one of these. At this latitude 0.001 degrees of latitude is
# 111.0 m, which is what sizes the offsets below.
LAT = 34.5000
LON = -118.5000

# 0 m, 44 m and 167 m from the centre. The middle one is inside a 100 m
# geofence and the last is outside it: the discriminating pair, without
# which "the backfill ran" and "the backfill took everything" look alike.
NEAR = (34.5000, -118.5000)
INSIDE = (34.5004, -118.5000)
OUTSIDE = (34.5015, -118.5000)


def _seed(tmp_path, name, pings=None):
    """A location.db holding three unassigned pings and nothing else."""
    loc_db = tmp_path / f"{name}-location.db"
    location_db.init_db(loc_db)
    with location_db.connect(loc_db) as conn:
        for i, (lat, lon) in enumerate(pings if pings is not None
                                       else (NEAR, INSIDE, OUTSIDE)):
            location_db.insert_ping(
                conn, f"2026-09-{i + 1:02d}T12:00:00Z", lat, lon,
                accuracy=5.0, activity_type="stationary",
            )
        conn.commit()
    return loc_db


def _ping_places(loc_db) -> list[int | None]:
    """``place_id`` for every ping, oldest first."""
    with location_db.connect(loc_db) as conn:
        return [
            row["place_id"] for row in conn.execute(
                "SELECT place_id FROM location_pings ORDER BY timestamp"
            )
        ]


def _run_skill(fn, loc_db, **args):
    """Call a skill subcommand and parse the JSON it prints."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with patch.dict("os.environ", {"LOCATION_DB_PATH": str(loc_db)},
                        clear=False):
            fn(SimpleNamespace(**args))
    finally:
        sys.stdout = old_stdout
    return json.loads(captured.getvalue())


def _learn_args(**overrides):
    args = {
        "name": "hardware store", "category": "other", "radius": 100,
        "notes": None, "lat": None, "lon": None,
        "backfill": False, "from_cluster": False,
    }
    args.update(overrides)
    return args


def _update_args(**overrides):
    args = {
        "name": None, "id": None, "rename": None, "category": None,
        "radius": None, "notes": None, "lat": None, "lon": None,
        "backfill": False,
    }
    args.update(overrides)
    return args


class TestLearnSitesAPlaceFromCoordinates:
    """The filed workflow: name a stop hours after leaving it."""

    def test_given_coordinates_are_what_gets_saved(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "coords")
        out = _run_skill(cmd_learn, loc_db,
                         **_learn_args(lat=LAT, lon=LON, radius=50))

        assert out["lat"] == pytest.approx(LAT)
        assert out["lon"] == pytest.approx(LON)
        with location_db.connect(loc_db) as conn:
            place = location_db.get_place_by_name(conn, "hardware store")
        assert place.lat == pytest.approx(LAT)
        assert place.lon == pytest.approx(LON)

    def test_the_output_says_where_the_coordinates_came_from(self, tmp_path):
        """The issue's actual complaint.

        A `learn` that quietly sites the place at the phone's current
        position leaves a correctly named geofence on the wrong spot,
        and nothing in the output distinguishes that from the intended
        one.
        """
        from istota.skills.location import cmd_learn

        given = _run_skill(cmd_learn, _seed(tmp_path, "a"),
                           **_learn_args(lat=LAT, lon=LON))
        latest = _run_skill(cmd_learn, _seed(tmp_path, "b"), **_learn_args())

        assert given["source"] == "argument"
        assert latest["source"] == "latest_ping"

    def test_no_coordinates_still_saves_the_newest_ping(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "latest")
        out = _run_skill(cmd_learn, loc_db, **_learn_args())

        # OUTSIDE is the newest of the three seeded pings.
        assert out["lat"] == pytest.approx(OUTSIDE[0])
        assert out["lon"] == pytest.approx(OUTSIDE[1])

    def test_one_coordinate_without_the_other_is_refused(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "half")
        with pytest.raises(SystemExit):
            _run_skill(cmd_learn, loc_db, **_learn_args(lat=LAT))
        with location_db.connect(loc_db) as conn:
            assert location_db.get_place_by_name(conn, "hardware store") is None


class TestTheBackfillIsOptInAndBounded:
    def test_backfill_adopts_only_the_pings_inside_the_radius(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "backfill")
        out = _run_skill(cmd_learn, loc_db,
                         **_learn_args(lat=LAT, lon=LON, radius=100,
                                       backfill=True))

        assert out["backfilled_pings"] == 2
        with location_db.connect(loc_db) as conn:
            place = location_db.get_place_by_name(conn, "hardware store")
        assert _ping_places(loc_db) == [place.id, place.id, None]

    def test_without_the_flag_history_is_left_alone(self, tmp_path):
        """It rewrites history, so the CLI asks before doing it."""
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "nobackfill")
        out = _run_skill(cmd_learn, loc_db,
                         **_learn_args(lat=LAT, lon=LON, radius=100))

        assert out["backfilled_pings"] is None
        assert out["released_pings"] is None
        assert _ping_places(loc_db) == [None, None, None]

    def test_relearning_a_name_moves_the_place_and_reports_the_release(
        self, tmp_path,
    ):
        """`upsert_place` is ON CONFLICT DO UPDATE, so `learn` is a move.

        The release half then fires on a verb whose name suggests only
        adoption. Reporting the adopt count alone said "nothing happened
        to history" while detaching every ping the old footprint held.
        """
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "relearn")
        first = _run_skill(cmd_learn, loc_db,
                           **_learn_args(lat=LAT, lon=LON, radius=100,
                                         backfill=True))
        assert (first["backfilled_pings"], first["released_pings"]) == (2, 0)
        with location_db.connect(loc_db) as conn:
            place_id = location_db.get_place_by_name(conn, "hardware store").id
        assert _ping_places(loc_db) == [place_id, place_id, None]

        # Same name, somewhere else entirely: both pings are detached.
        second = _run_skill(cmd_learn, loc_db,
                            **_learn_args(lat=40.0, lon=-74.0, radius=100,
                                          backfill=True))

        assert (second["backfilled_pings"], second["released_pings"]) == (0, 2)
        assert _ping_places(loc_db) == [None, None, None]

    def test_an_out_of_range_coordinate_is_refused(self, tmp_path):
        """Nothing downstream refuses one — haversine does not raise."""
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "range")
        for bad in ({"lat": 200.0, "lon": -118.5}, {"lat": 34.5, "lon": 900.0}):
            with pytest.raises(SystemExit):
                _run_skill(cmd_learn, loc_db, **_learn_args(**bad))
        with location_db.connect(loc_db) as conn:
            assert location_db.get_place_by_name(conn, "hardware store") is None
        assert _ping_places(loc_db) == [None, None, None]


class TestUpdateReassignsPings:
    """`cmd_update` used to write the new geometry and stop there.

    Moving a place from the CLI therefore left its pings attached to the
    old footprint, where the web route had always reassigned them.
    """

    def test_moving_a_place_releases_the_pings_it_no_longer_contains(
        self, tmp_path,
    ):
        from istota.skills.location import cmd_learn, cmd_update

        loc_db = _seed(tmp_path, "move")
        _run_skill(cmd_learn, loc_db,
                   **_learn_args(lat=LAT, lon=LON, radius=100, backfill=True))
        with location_db.connect(loc_db) as conn:
            place_id = location_db.get_place_by_name(conn, "hardware store").id
        assert _ping_places(loc_db) == [place_id, place_id, None]

        # Shrink the geofence to 20 m: only the ping at the centre survives.
        out = _run_skill(cmd_update, loc_db,
                         **_update_args(id=place_id, radius=20, backfill=True))

        assert out["reassigned_pings"] == {"assigned": 0, "released": 1}
        assert _ping_places(loc_db) == [place_id, None, None]

    def test_widening_the_radius_adopts_what_now_falls_inside(self, tmp_path):
        from istota.skills.location import cmd_learn, cmd_update

        loc_db = _seed(tmp_path, "widen")
        _run_skill(cmd_learn, loc_db,
                   **_learn_args(lat=LAT, lon=LON, radius=100, backfill=True))
        with location_db.connect(loc_db) as conn:
            place_id = location_db.get_place_by_name(conn, "hardware store").id

        out = _run_skill(cmd_update, loc_db,
                         **_update_args(id=place_id, radius=300,
                                        backfill=True))

        assert out["reassigned_pings"] == {"assigned": 1, "released": 0}
        assert _ping_places(loc_db) == [place_id] * 3

    def test_without_the_flag_a_move_leaves_the_pings_alone(self, tmp_path):
        """The default branch, and what the CHANGELOG "Fixed" bullet sells.

        `learn` has the mirror of this; `update` did not, so the opt-in
        half of the new behaviour was uncovered.
        """
        from istota.skills.location import cmd_learn, cmd_update

        loc_db = _seed(tmp_path, "noflag")
        _run_skill(cmd_learn, loc_db,
                   **_learn_args(lat=LAT, lon=LON, radius=100, backfill=True))
        with location_db.connect(loc_db) as conn:
            place_id = location_db.get_place_by_name(conn, "hardware store").id

        out = _run_skill(cmd_update, loc_db,
                         **_update_args(id=place_id, radius=20))

        assert out["reassigned_pings"] is None
        assert _ping_places(loc_db) == [place_id, place_id, None]

    def test_a_non_geometric_edit_touches_no_ping(self, tmp_path):
        from istota.skills.location import cmd_learn, cmd_update

        loc_db = _seed(tmp_path, "rename")
        _run_skill(cmd_learn, loc_db,
                   **_learn_args(lat=LAT, lon=LON, radius=100, backfill=True))
        with location_db.connect(loc_db) as conn:
            place_id = location_db.get_place_by_name(conn, "hardware store").id

        out = _run_skill(cmd_update, loc_db,
                         **_update_args(id=place_id, rename="REI",
                                        backfill=True))

        assert out["reassigned_pings"] is None
        assert _ping_places(loc_db) == [place_id, place_id, None]


class TestLearnFromADiscoveredCluster:
    """A cluster's fitted centroid beats coordinates read off a summary.

    ``discover`` already weights the centroid by ping count and fits the
    radius to the observed spread; a coordinate copied out of a day
    summary is one ping's position, which has put a saved place ~13 m
    off the true centre.
    """

    CLUSTER = [(34.5000 + 0.00002 * i, -118.5000 + 0.00002 * i)
               for i in range(12)]

    def test_the_cluster_centroid_and_radius_are_adopted(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "cluster", pings=self.CLUSTER)
        # A point near the cluster, not on its centroid.
        out = _run_skill(cmd_learn, loc_db,
                         **_learn_args(lat=34.50000, lon=-118.50000,
                                       from_cluster=True, radius=100))

        assert out["source"] == "cluster"
        centroid_lat = sum(p[0] for p in self.CLUSTER) / len(self.CLUSTER)
        assert out["lat"] == pytest.approx(centroid_lat, abs=1e-5)
        # The fitted radius, not the --radius default.
        assert out["radius_meters"] == out["cluster"]["radius_meters"]

    def test_no_cluster_near_the_point_is_an_error(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "nocluster", pings=self.CLUSTER)
        with pytest.raises(SystemExit):
            _run_skill(cmd_learn, loc_db,
                       **_learn_args(lat=40.0, lon=-74.0, from_cluster=True))

    def test_from_cluster_needs_a_point_to_resolve_against(self, tmp_path):
        from istota.skills.location import cmd_learn

        loc_db = _seed(tmp_path, "nopoint", pings=self.CLUSTER)
        with pytest.raises(SystemExit):
            _run_skill(cmd_learn, loc_db, **_learn_args(from_cluster=True))


class TestTheHelperItself:
    def test_a_ping_just_outside_the_radius_is_left_alone(self, tmp_path):
        from istota.location_logic import assign_pings_to_place

        loc_db = _seed(tmp_path, "helper")
        with location_db.connect(loc_db) as conn:
            place_id = location_db.add_place(
                conn, "x", LAT, LON, radius_meters=100,
            )
            counts = assign_pings_to_place(conn, place_id, LAT, LON, 100)
            conn.commit()

        assert counts == {"assigned": 2, "released": 0}
        assert _ping_places(loc_db) == [place_id, place_id, None]

    def test_a_ping_attached_to_another_place_is_not_stolen(self, tmp_path):
        """Only unattached pings are adopted.

        The bounding box is centred on the new place, so a neighbour's
        assigned pings sit inside it whenever the two circles overlap.
        """
        from istota.location_logic import assign_pings_to_place

        loc_db = _seed(tmp_path, "steal")
        with location_db.connect(loc_db) as conn:
            other = location_db.add_place(
                conn, "neighbour", INSIDE[0], INSIDE[1], radius_meters=20,
            )
            conn.execute(
                "UPDATE location_pings SET place_id = ? WHERE lat = ?",
                (other, INSIDE[0]),
            )
            mine = location_db.add_place(
                conn, "mine", LAT, LON, radius_meters=100,
            )
            counts = assign_pings_to_place(conn, mine, LAT, LON, 100)
            conn.commit()

        assert counts == {"assigned": 1, "released": 0}
        assert _ping_places(loc_db) == [mine, other, None]


@pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
class TestTheTwoSurfacesWriteTheSameGeofence:
    """Both create paths land the same pings on the same place.

    The web route's backfill is where this logic was written; the point
    of lifting it into ``location_logic`` is that the skill now runs the
    identical code rather than a second copy of it.
    """

    def test_learn_with_backfill_matches_the_web_create_route(self, tmp_path):
        from istota.skills.location import cmd_learn
        from istota.web_app import _location_create_place

        skill_db = _seed(tmp_path, "skill")
        web_db = _seed(tmp_path, "web")

        skill = _run_skill(cmd_learn, skill_db,
                           **_learn_args(lat=LAT, lon=LON, radius=100,
                                         backfill=True))
        web = _location_create_place(str(web_db), {
            "name": "hardware store", "lat": LAT, "lon": LON,
            "radius_meters": 100, "category": "other",
        })

        assert skill["backfilled_pings"] == web["backfilled_pings"] == 2
        assert _ping_places(skill_db) == _ping_places(web_db)

    def test_update_with_backfill_matches_the_web_update_route(self, tmp_path):
        from istota.skills.location import cmd_learn, cmd_update
        from istota.web_app import _location_create_place, _location_update_place

        skill_db = _seed(tmp_path, "skill2")
        web_db = _seed(tmp_path, "web2")

        _run_skill(cmd_learn, skill_db,
                   **_learn_args(lat=LAT, lon=LON, radius=100, backfill=True))
        created = _location_create_place(str(web_db), {
            "name": "hardware store", "lat": LAT, "lon": LON,
            "radius_meters": 100, "category": "other",
        })

        with location_db.connect(skill_db) as conn:
            skill_place = location_db.get_place_by_name(conn, "hardware store").id
        _run_skill(cmd_update, skill_db,
                   **_update_args(id=skill_place, radius=20, backfill=True))
        _location_update_place(str(web_db), created["id"], {"radius_meters": 20})

        assert _ping_places(skill_db) == _ping_places(web_db)
