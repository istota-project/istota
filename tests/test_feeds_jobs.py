"""Tests for feeds module job seeding into istota's scheduler."""

from pathlib import Path

import pytest

from istota import db
from istota.config import Config, ResourceConfig, UserConfig
from istota.cron_loader import _MODULE_JOB_PREFIX, sync_cron_jobs_to_db
from istota.feeds.jobs import DEFAULT_JOBS, MODULE_PREFIX, jobs_for_user
from istota.feeds.workspace import synthesize_feeds_context
from istota.scheduler import _sync_feeds_module_jobs


def _conn(tmp_path: Path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_app_config(
    tmp_path: Path,
    users: dict[str, list[ResourceConfig]],
    *,
    nextcloud_mount: Path | None = None,
) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=nextcloud_mount or tmp_path,
        users={
            uid: UserConfig(resources=resources)
            for uid, resources in users.items()
        },
    )


# ---------------------------------------------------------------------------
# jobs_for_user — pure logic
# ---------------------------------------------------------------------------


class TestJobsForUser:
    def test_seeds_run_scheduled(self, tmp_path):
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        names = [j["name"] for j in jobs]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_returns_empty_when_no_context(self):
        assert jobs_for_user(None, "alice") == []

    def test_command_uses_istota_skill_with_feeds_user(self, tmp_path):
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        for j in jobs:
            assert "FEEDS_USER=alice" in j["command"]
            assert "istota-skill feeds run-scheduled" in j["command"]

    def test_default_cron_every_5_min(self, tmp_path):
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        assert jobs[0]["cron"] == "*/5 * * * *"


# ---------------------------------------------------------------------------
# _sync_feeds_module_jobs — DB integration
# ---------------------------------------------------------------------------


class TestSyncFeedsModuleJobs:
    def test_seeds_for_user_with_feeds_resource(self, tmp_path):
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        # Make sure the user has a bot dir (storage helper expects path)
        import istota.storage as storage  # noqa: F401
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT name FROM scheduled_jobs WHERE user_id = ? ORDER BY name",
            ("alice",),
        ).fetchall()
        names = [r[0] for r in rows]
        assert names == [f"{MODULE_PREFIX}run_scheduled"]

    def test_user_without_feeds_resource_has_no_module_jobs(self, tmp_path):
        app_config = _make_app_config(tmp_path, {"bob": []})
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("bob", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_idempotent_no_duplicate_inserts(self, tmp_path):
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        _sync_feeds_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0]
        assert count == 1

    def test_removes_module_jobs_when_resource_disappears(self, tmp_path):
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        # Drop the resource
        app_config2 = _make_app_config(tmp_path, {"alice": []})
        _sync_feeds_module_jobs(conn, app_config2)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_seeds_with_skip_log_channel_set(self, tmp_path):
        # Module jobs run on a noisy cadence (*/5 min) and emit structured
        # JSON envelopes — they must never post to the user's log channel.
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT skip_log_channel FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1

    def test_backfills_skip_log_channel_on_existing_row(self, tmp_path):
        # Pre-existing module rows that were seeded before the fix have
        # skip_log_channel=0 and should be flipped to 1 on the next sync.
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        conn = _conn(tmp_path)
        # Simulate the pre-fix row shape.
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, enabled, "
            "skip_log_channel) "
            "VALUES (?, ?, ?, '', ?, 1, 0)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT skip_log_channel FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1

    def test_updates_when_command_changes(self, tmp_path):
        app_config = _make_app_config(
            tmp_path,
            {"alice": [ResourceConfig(type="feeds")]},
        )
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        # Manually mangle the command to simulate drift
        conn.execute(
            "UPDATE scheduled_jobs SET command = 'OLD' WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT command FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] != "OLD"
        assert "FEEDS_USER=alice" in row[0]


# ---------------------------------------------------------------------------
# CRON.md sync must not touch _module.feeds.* jobs
# ---------------------------------------------------------------------------


class TestCronMdLeavesFeedsModuleJobsAlone:
    def test_cron_md_orphan_pass_does_not_delete_module_jobs(self, tmp_path):
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, command) "
            "VALUES (?, ?, ?, '', ?)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/15 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled"),
        )
        conn.commit()
        sync_cron_jobs_to_db(conn, "alice", [])
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchall()
        assert len(rows) == 1
