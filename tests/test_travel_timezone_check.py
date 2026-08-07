"""ISSUE-096 — the scheduler half of following the timezone on travel.

Detection lives in `istota.location.timezone` and is tested separately. This is
the policy around it: who it runs for, what it writes, and what it tells them.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

try:
    import timezonefinder  # noqa: F401
    _has_tzf = True
except ImportError:
    _has_tzf = False

_needs_tzf = pytest.mark.skipif(not _has_tzf, reason="timezonefinder not installed")

from istota import db, user_profiles
from istota.config import Config, UserConfig
from istota.location import db as location_db
from istota.scheduler import check_travel_timezone


WARSAW = (52.16, 20.97)
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _config(tmp_path, *, user="alice", follow=True, timezone_name="America/Los_Angeles"):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)

    config = Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path,
        users={user: UserConfig()},
    )

    user_profiles.ensure_profile(db_path, user, timezone=timezone_name)
    user_profiles.update_profile(db_path, user, timezone_follow_location=follow)
    return config


def _seed_location_db(config, user, latlon, *, minutes_ago=(180, 90, 2)):
    from istota.location import resolve_for_user

    ctx = resolve_for_user(user, config)
    location_db.init_db(ctx.db_path)
    with location_db.connect(ctx.db_path) as conn:
        for minutes in minutes_ago:
            ts = (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            location_db.insert_ping(
                conn, ts, latlon[0], latlon[1], accuracy=8.0,
                activity_type="stationary",
            )
        conn.commit()
    return ctx.db_path


class TestTravelTimezoneGating:
    """The consent invariants. Deliberately outside the `timezonefinder` gate:
    "never rewrite a timezone the user did not opt into" is the whole safety
    argument for this feature, and it must be checked on a lean install too."""

    def _detects(self, zone="Europe/Warsaw"):
        return patch(
            "istota.location.timezone.detect_travel_timezone", return_value=zone,
        )

    def test_does_nothing_when_the_user_has_not_opted_in(self, tmp_path):
        config = _config(tmp_path, follow=False)
        _seed_location_db(config, "alice", WARSAW)

        with self._detects(), patch("istota.scheduler.send_notification") as notify:
            changed = check_travel_timezone(config, now=NOW)

        assert changed == []
        assert user_profiles.get_profile(
            config.db_path, "alice",
        ).timezone == "America/Los_Angeles"
        assert notify.call_count == 0

    def test_does_nothing_when_the_location_module_is_off(self, tmp_path):
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)
        user_profiles.update_profile(
            config.db_path, "alice", disabled_modules=["location"],
        )

        with self._detects(), patch("istota.scheduler.send_notification") as notify:
            changed = check_travel_timezone(config, now=NOW)

        assert changed == []
        assert notify.call_count == 0

    def test_a_user_with_no_location_db_is_skipped(self, tmp_path):
        config = _config(tmp_path)

        with self._detects():
            changed = check_travel_timezone(config, now=NOW)

        assert changed == []

    def test_the_field_defaults_to_off(self, tmp_path):
        """An existing row migrated by the ALTER must not start following."""
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "carol")

        assert user_profiles.get_profile(
            db_path, "carol",
        ).timezone_follow_location is False


@_needs_tzf
class TestCheckTravelTimezone:
    def test_writes_the_new_zone_and_tells_the_user(self, tmp_path):
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)

        with patch("istota.scheduler.send_notification") as notify:
            changed = check_travel_timezone(config, now=NOW)

        assert changed == [("alice", "Europe/Warsaw")]
        profile = user_profiles.get_profile(config.db_path, "alice")
        assert profile.timezone == "Europe/Warsaw"

        # Never silent: the setting it rewrote is one the user chose.
        assert notify.call_count == 1
        message = notify.call_args.args[2]
        assert "Europe/Warsaw" in message
        assert "America/Los_Angeles" in message

    def test_is_quiet_when_the_user_has_not_moved(self, tmp_path):
        config = _config(tmp_path, timezone_name="Europe/Warsaw")
        _seed_location_db(config, "alice", WARSAW)

        with patch("istota.scheduler.send_notification") as notify:
            changed = check_travel_timezone(config, now=NOW)

        assert changed == []
        assert notify.call_count == 0

    def test_one_user_raising_does_not_stop_the_rest(self, tmp_path):
        config = _config(tmp_path)
        config.users["bob"] = UserConfig()
        user_profiles.ensure_profile(config.db_path, "bob", timezone="UTC")
        user_profiles.update_profile(
            config.db_path, "bob", timezone_follow_location=True,
        )
        _seed_location_db(config, "alice", WARSAW)
        _seed_location_db(config, "bob", WARSAW)

        real_connect = location_db.connect

        def explode_for_alice(path, *a, **kw):
            if "alice" in str(path):
                raise sqlite3.OperationalError("disk I/O error")
            return real_connect(path, *a, **kw)

        with patch("istota.location.db.connect", side_effect=explode_for_alice), \
                patch("istota.scheduler.send_notification") as notify:
            changed = check_travel_timezone(config, now=NOW)

        # bob still gets his change, and the user who blew up is told nothing.
        assert changed == [("bob", "Europe/Warsaw")]
        assert [c.args[1] for c in notify.call_args_list] == ["bob"]

    def test_does_not_rewrite_the_same_zone_again(self, tmp_path):
        """Detection is memoryless, so without a record of what it last set a
        user who prefers home time abroad is overridden on every tick."""
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)

        with patch("istota.scheduler.send_notification"):
            first = check_travel_timezone(config, now=NOW)
            user_profiles.update_profile(
                config.db_path, "alice", timezone="America/Los_Angeles",
            )
            second = check_travel_timezone(config, now=NOW + timedelta(minutes=15))

        assert first == [("alice", "Europe/Warsaw")]
        assert second == []
        assert user_profiles.get_profile(
            config.db_path, "alice",
        ).timezone == "America/Los_Angeles"

    def test_the_cooldown_lapses(self, tmp_path):
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)
        # Still in Warsaw a day later — the track has to be fresh at the later
        # moment too, or the staleness gate rejects it before the cooldown does.
        _seed_location_db(
            config, "alice", WARSAW,
            minutes_ago=(-60 * 25 + 180, -60 * 25 + 90, -60 * 25 + 2),
        )

        with patch("istota.scheduler.send_notification"):
            check_travel_timezone(config, now=NOW)
            user_profiles.update_profile(
                config.db_path, "alice", timezone="America/Los_Angeles",
            )
            later = check_travel_timezone(config, now=NOW + timedelta(hours=25))

        assert later == [("alice", "Europe/Warsaw")]

    def test_an_undelivered_notice_still_keeps_the_write(self, tmp_path):
        """`send_notification` returns False rather than raising when nothing
        accepts — a standalone install with Talk off, say."""
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)

        with patch("istota.scheduler.send_notification", return_value=False):
            changed = check_travel_timezone(config, now=NOW)

        assert changed == [("alice", "Europe/Warsaw")]

    def test_a_failed_notification_still_keeps_the_write(self, tmp_path):
        """The timezone is what makes the user's clocks right; the notice is
        courtesy. A dead Talk room must not roll the change back."""
        config = _config(tmp_path)
        _seed_location_db(config, "alice", WARSAW)

        with patch(
            "istota.scheduler.send_notification", side_effect=RuntimeError("talk down"),
        ):
            changed = check_travel_timezone(config, now=NOW)

        assert changed == [("alice", "Europe/Warsaw")]
        assert user_profiles.get_profile(
            config.db_path, "alice",
        ).timezone == "Europe/Warsaw"
