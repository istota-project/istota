"""Tests for feeds module job seeding into istota's scheduler."""

from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
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
    user_ids: list[str],
    *,
    nextcloud_mount: Path | None = None,
    disabled_modules: dict[str, list[str]] | None = None,
) -> Config:
    """Build a Config with the given users.

    Module gating is on by default (``Config.is_module_enabled`` returns True
    unless the user is in ``disabled_modules``); a per-user opt-out goes in
    the ``disabled_modules`` mapping.
    """
    disabled_modules = disabled_modules or {}
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=nextcloud_mount or tmp_path,
        users={
            uid: UserConfig(disabled_modules=disabled_modules.get(uid, []))
            for uid in user_ids
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

    def test_dispatch_shape_is_skill_task(self, tmp_path):
        """Phase 1.3: jobs are skill-tasks, not shell command-tasks. The
        master Fernet key never enters the subprocess env on this path."""
        import json
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        for j in jobs:
            assert "command" not in j
            assert j["skill"] == "feeds"
            assert json.loads(j["skill_args"]) == ["run-scheduled"]

    def test_default_cron_every_5_min(self, tmp_path):
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        assert jobs[0]["cron"] == "*/5 * * * *"


# ---------------------------------------------------------------------------
# _sync_feeds_module_jobs — DB integration
# ---------------------------------------------------------------------------


class TestSyncFeedsModuleJobs:
    def test_seeds_for_user_with_feeds_resource(self, tmp_path):
        app_config = _make_app_config(tmp_path, ["alice"])
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

    def test_user_with_feeds_module_disabled_has_no_module_jobs(self, tmp_path):
        app_config = _make_app_config(
            tmp_path, ["bob"],
            disabled_modules={"bob": ["feeds"]},
        )
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("bob", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_idempotent_no_duplicate_inserts(self, tmp_path):
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        _sync_feeds_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0]
        assert count == 1

    def test_removes_module_jobs_when_module_disabled(self, tmp_path):
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        # Disable the feeds module for alice
        app_config2 = _make_app_config(
            tmp_path, ["alice"],
            disabled_modules={"alice": ["feeds"]},
        )
        _sync_feeds_module_jobs(conn, app_config2)
        rows = conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert rows == []

    def test_seeds_with_skip_log_channel_set(self, tmp_path):
        # Module jobs run on a noisy cadence (*/5 min) and emit structured
        # JSON envelopes — they must never post to the user's log channel.
        app_config = _make_app_config(tmp_path, ["alice"])
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
        # Critically, backfilling must NOT bump last_run_at — doing so would
        # defer the next scheduled run by one full cron interval.
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        original_last_run = "2026-05-03 07:55:00"
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, enabled, "
            "skip_log_channel, last_run_at) "
            "VALUES (?, ?, ?, '', ?, 1, 0, ?)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled",
             original_last_run),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT skip_log_channel, last_run_at FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == original_last_run

    def test_first_seed_queues_immediate_poll_task(self, tmp_path):
        # Newly provisioned users shouldn't have to wait up to 5 minutes for
        # the first cron tick — first seed enqueues a one-shot skill task.
        import json
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT skill, skill_args, command, queue, source_type, "
            "skip_log_channel FROM tasks WHERE user_id = ?",
            ("alice",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "feeds"
        assert json.loads(rows[0][1]) == ["run-scheduled"]
        assert rows[0][2] is None
        assert rows[0][3] == "background"
        assert rows[0][4] == "scheduled"
        assert rows[0][5] == 1

    def test_resync_does_not_requeue_immediate_poll(self, tmp_path):
        # Subsequent restarts must not flood the queue — the immediate-poll
        # task is a one-time hook, only fired when the job row is absent.
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        _sync_feeds_module_jobs(conn, app_config)
        _sync_feeds_module_jobs(conn, app_config)
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ?", ("alice",),
        ).fetchone()[0]
        assert count == 1

    def test_migrates_legacy_command_row_to_skill_shape(self, tmp_path):
        """Pre-Phase-1.3 hosts have command-shape rows; the next sync
        rewrites them to the skill/skill_args shape and clears command."""
        import json
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        # Pre-seed a legacy command-shape row
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel) "
            "VALUES (?, ?, ?, '', ?, NULL, NULL, 1, 1)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT command, skill, skill_args FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "feeds"
        assert json.loads(row[2]) == ["run-scheduled"]

    def test_rescues_post_migration_auto_disabled_row(self, tmp_path):
        """Sequence: row was first migrated from command→skill shape,
        then auto-disabled because in-flight tasks created before the
        migration kept hitting the admin gate. Next sync must un-stick
        the row instead of leaving it paused forever."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        # Already-migrated shape (command=NULL, skill set) but
        # enabled=0 + the admin-gate failure recorded.
        # last_run_at is old enough to clear the 1h cooldown.
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 6, ?, "
            "datetime('now', '-2 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "command-type tasks are admin-only"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures, last_error "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 0
        assert row[2] is None

    def test_rescues_row_disabled_by_non_admin_gate_failure(self, tmp_path):
        """Wave 2: post-cc0bd54-but-pre-027eb1a, claim_task didn't return
        the skill columns, so module rows fell through to the LLM path
        with an empty prompt and accumulated 5 timeouts / malformed-output
        failures before auto-disabling. The rescue can't key on a single
        error string for this wave — any auto-disabled module row whose
        last_run_at predates the 1h cooldown gets rescued."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 5, ?, "
            "datetime('now', '-9 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "Task timed out after 30 minutes"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures, last_error "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 0
        assert row[2] is None

    def test_rescue_skips_recently_disabled_row(self, tmp_path):
        """1h cooldown gate: a row that was auto-disabled within the last
        hour stays disabled. This caps the rescue→fail→rescue loop rate
        for genuinely broken rows — without it, every */5 cron tick we'd
        retry 5 more times and re-disable, indefinitely."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 5, ?, "
            "datetime('now', '-10 minutes'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "Task timed out after 30 minutes"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 5

    def test_legacy_command_migration_also_clears_auto_disable(self, tmp_path):
        """One-step migration path: the row is still in command shape AND
        has been auto-disabled by the admin gate. The drift-driven update
        must do both — even when last_run_at is recent (the broad rescue
        skips inside the 1h cooldown, but legacy_command drift fires
        unconditionally)."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at) "
            "VALUES (?, ?, ?, '', ?, NULL, NULL, 0, 1, 6, ?, "
            "datetime('now', '-10 minutes'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled",
             "command-type tasks are admin-only"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT command, skill, enabled, consecutive_failures, last_error "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "feeds"
        assert row[2] == 1
        assert row[3] == 0
        assert row[4] is None

    def test_rescue_does_not_touch_operator_paused_row(self, tmp_path):
        """consecutive_failures=0 means the row was paused by an operator
        (or never failed), not auto-disabled. Module rows have no
        operator-pause UI today, but the rescue still respects the
        distinguisher so a future surface — or a direct DB edit — won't
        get clobbered."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 0, NULL, "
            "datetime('now', '-9 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 0


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
