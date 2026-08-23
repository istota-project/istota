"""The two scheduler-side `task_alert` producers, and the sweeps behind them.

Both are one-shot notices about something that has already happened, and both had
exactly one delivery attempt and nothing behind it:

- an expired confirmation (`run_cleanup_checks`) tells the user what was dropped
  and leaves nothing. Its send carries a `conversation_token` override the store
  does not model, so the send stays where it is and the row records it.
- an undeliverable task result (`process_one_task`) is the last channel a task
  with no room leg has. `send_notification` returns False with no destination
  configured, which is how the answer used to disappear on the one path whose
  whole purpose is that it does not.

`run_cleanup_checks` is also where both sweeps run, and this stage is what makes
`sweep_expired_alerts` non-trivial — before `task_alert` registered, no source
declared `auto_resolve_on_seen` and the pass closed nothing by construction.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.notification_resolvers import task_alert
from istota.scheduler import _write_undelivered_row, run_cleanup_checks


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url="https://nc.example.com"),
        talk=TalkConfig(enabled=False),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(confirmation_timeout_minutes=1),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(display_name="Alice", alerts_channel="alerts")},
    )
    cfg.temp_dir.mkdir()
    db.init_db(cfg.db_path)
    return cfg


def _rows(config, prefix=""):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = 'task_alert' "
            "AND dedup_key LIKE ? ORDER BY id", (f"{prefix}%",),
        ).fetchall()


def _held_task(config, *, minutes_ago: int) -> int:
    """A task parked at `pending_confirmation` and aged past the timeout."""
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt="Do the thing", user_id="alice", source_type="talk",
            conversation_token="room-1",
        )
        db.set_task_confirmation(conn, task_id, "May I?")
        # `expire_stale_confirmations` ages on `updated_at`.
        conn.execute(
            "UPDATE tasks SET updated_at = datetime('now', ?) WHERE id = ?",
            (f"-{minutes_ago} minutes", task_id),
        )
    return task_id


class TestTheExpiredConfirmationNotice:
    def test_expiring_a_confirmation_raises_a_row(self, config):
        task_id = _held_task(config, minutes_ago=90)

        with patch("istota.scheduler.send_notification", return_value=True) as send:
            run_cleanup_checks(config)

        assert send.call_count == 1
        rows = _rows(config, "expired:")
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == task_alert.expired_key(task_id)
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["state"] == "open"
        # Nothing to press: the task is cancelled and resubmitting happens
        # wherever the request came from.
        assert rows[0]["actionable"] == 0
        assert rows[0]["link"] is None

    def test_the_confirmation_row_is_closed_and_the_alert_row_is_not(self, config):
        """Two items, two lifecycles.

        Reusing the confirmation's key would reopen a resolved row, which would
        put a Confirm button back on a task that has already been cancelled.
        """
        from istota.notification_resolvers import confirmation as confirmation_source

        task_id = _held_task(config, minutes_ago=90)
        with db.get_db(config.db_path) as conn:
            confirmation_source.write(
                conn, "alice", task_id=task_id, title="A question", body="May I?",
            )

        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)

        with db.get_db(config.db_path) as conn:
            by_source = {
                r["source"]: r for r in conn.execute(
                    "SELECT * FROM notifications ORDER BY id",
                ).fetchall()
            }
        assert by_source["confirmation"]["state"] == "resolved"
        assert by_source["confirmation"]["resolved_by"] == "system"
        assert by_source["task_alert"]["state"] == "open"

    def test_a_delivery_that_reached_nobody_leaves_the_row(self, config):
        _held_task(config, minutes_ago=90)

        with patch("istota.scheduler.send_notification", return_value=False) as send:
            run_cleanup_checks(config)

        assert send.call_count == 1
        row = _rows(config, "expired:")[0]
        assert row["state"] == "open"
        assert row["last_delivered_at"] is None

    def test_a_successful_delivery_stamps_the_row(self, config):
        _held_task(config, minutes_ago=90)
        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)
        assert _rows(config, "expired:")[0]["last_delivered_at"] is not None

    def test_the_notice_still_names_what_was_dropped(self, config):
        _held_task(config, minutes_ago=90)
        with patch("istota.scheduler.send_notification", return_value=True) as send:
            run_cleanup_checks(config)
        # The send is unchanged, `conversation_token` override included — the row
        # is added beside it, not in place of it.
        assert send.call_args.kwargs["purpose"] == "alert"
        assert "conversation_token" in send.call_args.kwargs
        assert "timed out" in send.call_args.args[2]


class TestTheUndeliverableResultNotice:
    def test_a_row_is_written_with_no_link(self, config):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Reply to Bob", user_id="alice", source_type="email",
            )
            task = db.get_task(conn, task_id)

        notification_id = _write_undelivered_row(
            config, task, "Could not send the email reply",
            "The answer is below:\n\n**bold** [link](http://evil.example)",
        )
        assert notification_id is not None

        row = _rows(config, "undelivered:")[0]
        assert row["dedup_key"] == task_alert.undelivered_key(task_id)
        assert row["link"] is None
        assert row["state"] == "open"
        assert row["last_delivered_at"] is None
        # The body is the flattened record; the full answer stays in
        # `tasks.result` and in the send, which is unchanged.
        for char in "[]()*":
            assert char not in row["body"]

    def test_it_never_raises_into_the_task(self, config, caplog):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type="email",
            )
            task = db.get_task(conn, task_id)

        with (
            caplog.at_level("WARNING"),
            patch("istota.scheduler.db.get_db", side_effect=RuntimeError("locked")),
        ):
            assert _write_undelivered_row(config, task, "t", "b") is None
        assert "could not record the undelivered-result notification" in caplog.text


class TestTheSweepsRunInTheCleanupPass:
    def test_an_aged_alert_row_is_closed(self, config):
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice", dedup_key="dmarc:fail", title="Notice",
            )
            conn.execute(
                "UPDATE notifications SET updated_at = ? WHERE id = ?",
                (db.iso_utc_days_ago(store.NOTIFICATION_ALERT_MAX_AGE_DAYS + 1),
                 result.notification_id),
            )

        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)

        assert _rows(config, "dmarc:")[0]["state"] == "resolved"
        assert _rows(config, "dmarc:")[0]["resolved_by"] == "system"

    def test_a_recent_alert_row_is_left_alone(self, config):
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            task_alert.write(conn, "alice", dedup_key="dmarc:fail", title="Notice")

        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)

        assert _rows(config, "dmarc:")[0]["state"] == "open"

    def test_an_object_backed_row_is_never_swept(self, config):
        """Its close condition is the object, not the clock, at any age."""
        from istota.notification_resolvers import confirmation as confirmation_source

        sources.register(task_alert.RESOLVER)
        sources.register(confirmation_source.RESOLVER)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type="talk",
            )
            result = confirmation_source.write(
                conn, "alice", task_id=task_id, title="A question",
            )
            conn.execute(
                "UPDATE notifications SET updated_at = ? WHERE id = ?",
                (db.iso_utc_days_ago(400), result.notification_id),
            )

        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)

        with db.get_db(config.db_path) as conn:
            state = conn.execute(
                "SELECT state FROM notifications WHERE id = ?",
                (result.notification_id,),
            ).fetchone()[0]
        assert state == "open"

    def test_a_long_closed_row_is_deleted(self, config):
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice", dedup_key="dmarc:fail", title="Notice",
            )
            stamp = db.iso_utc_days_ago(store.NOTIFICATION_RETENTION_DAYS + 1)
            conn.execute(
                "UPDATE notifications SET state = 'resolved', resolved_at = ?, "
                "updated_at = ? WHERE id = ?",
                (stamp, stamp, result.notification_id),
            )

        with patch("istota.scheduler.send_notification", return_value=True):
            run_cleanup_checks(config)

        assert _rows(config, "dmarc:") == []

    def test_a_sweep_failure_never_fails_the_cleanup_pass(self, config, caplog):
        sources.register(task_alert.RESOLVER)
        with (
            caplog.at_level("WARNING"),
            patch("istota.scheduler.sweep_expired_alerts",
                  side_effect=RuntimeError("boom")),
            patch("istota.scheduler.send_notification", return_value=True),
        ):
            run_cleanup_checks(config)
        assert "Notification sweeps skipped" in caplog.text
