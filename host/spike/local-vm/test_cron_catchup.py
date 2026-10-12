"""local-vm-install Stage 1 (e): several missed slots of one job after a Mac
sleep, under `[scheduler] cron_max_staleness_minutes`.

Not part of the suite (it lives outside `tests/`); run by path:

    uv run pytest host/spike/local-vm/test_cron_catchup.py -q --no-header -p no:cacheprovider -n0

`set_*_last_run` stamps SQLite's own `datetime('now')`, not the patched
`_now`, so every case pins "now" to the real clock and places the missed slots
behind it.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import BriefingConfig, Config, SchedulerConfig, UserConfig
from istota.scheduler import check_briefings, check_scheduled_jobs


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "spike.db"
    db.init_db(path)
    return path


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sql(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _job(db_path, cron, last_run):
    with db.get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO scheduled_jobs
               (user_id, name, cron_expression, prompt, conversation_token, enabled, last_run_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("alice", "job", cron, "Run", "room1", 1, _sql(last_run), _sql(last_run)),
        )


def _ticks(db_path, config, now, offsets_s):
    """Run check_scheduled_jobs at now + each offset; return tasks created per tick."""
    created = []
    for offset in offsets_s:
        with patch("istota.scheduler._now", return_value=now + timedelta(seconds=offset)), \
                patch("istota.scheduler._sync_cron_files"):
            with db.get_db(db_path) as conn:
                created.append(len(check_scheduled_jobs(conn, config)))
    return created


def _config(db_path, minutes, briefings=()):
    return Config(
        db_path=db_path,
        users={"alice": UserConfig(timezone="UTC", briefings=list(briefings))},
        scheduler=SchedulerConfig(cron_max_staleness_minutes=minutes),
    )


def test_hourly_job_five_missed_slots_gives_one_run_at_720(db_path):
    now = _utc_now().replace(minute=30, second=0)
    if now > _utc_now():
        now -= timedelta(hours=1)
    _job(db_path, "0 * * * *", now - timedelta(hours=5, minutes=30))
    ticks = _ticks(db_path, _config(db_path, 720), now, [0, 30, 60, 90])
    print("hourly, 5 missed slots, 720:", ticks)
    assert ticks == [1, 0, 0, 0]


def test_hourly_job_five_missed_slots_gives_no_run_at_60(db_path):
    # The gate measures from the oldest missed slot, so even the slot 30 min
    # ago is skipped once the first one is over the threshold.
    now = _utc_now().replace(minute=30, second=0)
    if now > _utc_now():
        now -= timedelta(hours=1)
    _job(db_path, "0 * * * *", now - timedelta(hours=5, minutes=30))
    ticks = _ticks(db_path, _config(db_path, 60), now, [0, 30])
    print("hourly, 5 missed slots, 60:", ticks)
    assert ticks == [0, 0]


def test_five_minute_job_after_thirteen_hours_is_skipped_at_720(db_path):
    now = _utc_now()
    _job(db_path, "*/5 * * * *", now - timedelta(hours=13))
    ticks = _ticks(db_path, _config(db_path, 720), now, [0, 30])
    print("every 5 min, 13 h asleep, 720:", ticks)
    assert ticks == [0, 0]


def test_five_minute_job_after_two_hours_runs_once_at_720(db_path):
    now = _utc_now()
    _job(db_path, "*/5 * * * *", now - timedelta(hours=2))
    ticks = _ticks(db_path, _config(db_path, 720), now, [0, 30])
    print("every 5 min, 2 h asleep (24 slots), 720:", ticks)
    assert ticks == [1, 0]


def test_daily_briefing_missed_overnight_runs_once_at_720(db_path):
    now = _utc_now()
    slot = (now - timedelta(hours=9)).replace(second=0)
    briefing = BriefingConfig(
        name="morning", cron=f"{slot.minute} {slot.hour} * * *",
        conversation_token="room1", components={},
    )
    config = _config(db_path, 720, [briefing])
    with db.get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO briefing_state (user_id, briefing_name, last_run_at) VALUES (?, ?, ?)",
            ("alice", "morning", _sql(slot - timedelta(days=1))),
        )
    runs = []
    for offset in (0, 30):
        with patch("istota.scheduler._now", return_value=(now + timedelta(seconds=offset)).astimezone(ZoneInfo("UTC"))):
            runs.append(len(check_briefings(db_path, config)))
    print("daily briefing missed by 9 h, 720:", runs)
    assert runs == [1, 0]
