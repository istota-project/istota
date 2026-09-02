"""Tests for feeds module job seeding into istota's scheduler."""

import json
from pathlib import Path


from istota import db
from istota.config import Config, UserConfig
from istota.cron_loader import sync_cron_jobs_to_db
from istota.feeds.jobs import MODULE_PREFIX, jobs_for_user
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


def _job(jobs: list[dict], name: str) -> dict:
    """The rendered job with this name. Keyed by name rather than index so a
    reordering of DEFAULT_JOBS cannot silently move an assertion onto the
    other row."""
    matches = [j for j in jobs if j["name"] == name]
    assert len(matches) == 1, f"expected exactly one {name}, got {len(matches)}"
    return matches[0]



class TestJobsForUser:
    def test_seeds_run_scheduled_and_prune(self, tmp_path):
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        names = [j["name"] for j in jobs]
        assert names == [
            f"{MODULE_PREFIX}run_scheduled",
            f"{MODULE_PREFIX}prune",
        ]

    def test_returns_empty_when_no_context(self):
        assert jobs_for_user(None, "alice") == []

    def test_dispatch_shape_is_skill_task(self, tmp_path):
        """Phase 1.3: jobs are skill-tasks, not shell command-tasks. The
        master Fernet key never enters the subprocess env on this path."""
        import json
        ctx = synthesize_feeds_context("alice", tmp_path)
        jobs = jobs_for_user(ctx, "alice")
        assert jobs, "expected at least one rendered job"
        for j in jobs:
            assert "command" not in j
            assert j["skill"] == "feeds"
            assert isinstance(json.loads(j["skill_args"]), list)

    def test_poll_job_cron_and_args(self, tmp_path):
        import json
        ctx = synthesize_feeds_context("alice", tmp_path)
        job = _job(jobs_for_user(ctx, "alice"), f"{MODULE_PREFIX}run_scheduled")
        assert job["cron"] == "*/5 * * * *"
        assert json.loads(job["skill_args"]) == ["run-scheduled"]

    def test_prune_job_is_daily_and_takes_no_flags(self, tmp_path):
        """The prune row's exact shape. The cron is the contract with the
        scheduler and the args are the contract with the skill facade: a
        stray flag here would be dispatched to `feeds prune` on every
        deployment with the module enabled, unattended and daily."""
        import json
        ctx = synthesize_feeds_context("alice", tmp_path)
        job = _job(jobs_for_user(ctx, "alice"), f"{MODULE_PREFIX}prune")
        assert job["cron"] == "17 3 * * *"
        assert job["skill"] == "feeds"
        assert json.loads(job["skill_args"]) == ["prune"]

    def test_the_prune_job_never_runs_in_dry_run(self, tmp_path):
        """A scheduled dry run would report deletions and delete nothing,
        so growth would continue while the log said it was handled."""
        import json
        ctx = synthesize_feeds_context("alice", tmp_path)
        job = _job(jobs_for_user(ctx, "alice"), f"{MODULE_PREFIX}prune")
        assert "--dry-run" not in json.loads(job["skill_args"])


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
            "SELECT name, cron_expression, skill, skill_args FROM scheduled_jobs "
            "WHERE user_id = ? ORDER BY name",
            ("alice",),
        ).fetchall()
        assert [r[0] for r in rows] == [
            f"{MODULE_PREFIX}prune",
            f"{MODULE_PREFIX}run_scheduled",
        ]
        by_name = {r[0]: r for r in rows}
        assert by_name[f"{MODULE_PREFIX}prune"][1] == "17 3 * * *"
        assert by_name[f"{MODULE_PREFIX}prune"][2] == "feeds"
        assert json.loads(by_name[f"{MODULE_PREFIX}prune"][3]) == ["prune"]
        assert by_name[f"{MODULE_PREFIX}run_scheduled"][1] == "*/5 * * * *"

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
        names = [r[0] for r in conn.execute(
            "SELECT name FROM scheduled_jobs WHERE user_id = ? AND name LIKE ? "
            "ORDER BY name",
            ("alice", f"{MODULE_PREFIX}%"),
        )]
        assert names == [
            f"{MODULE_PREFIX}prune",
            f"{MODULE_PREFIX}run_scheduled",
        ]

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
        rows = conn.execute(
            "SELECT name, skip_log_channel FROM scheduled_jobs "
            "WHERE user_id = ? AND name LIKE ? ORDER BY name",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchall()
        assert len(rows) == 2
        assert all(r[1] == 1 for r in rows), dict(rows)

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
            "WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
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

    def test_upgrade_seeds_prune_beside_an_existing_poll_row(self, tmp_path):
        """The reconciliation path every existing deployment takes: the poll
        row is already there in its current shape, and the sync has to add the
        prune row without rewriting the poll row. `last_run_at` is the one to
        watch — bumping it would defer the next poll by a full interval."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        original_last_run = "2026-05-03 07:55:00"
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, last_run_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1, ?)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]', original_last_run),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        rows = {
            r[0]: r for r in conn.execute(
                "SELECT name, cron_expression, skill_args, last_run_at, "
                "skip_log_channel FROM scheduled_jobs "
                "WHERE user_id = ? AND name LIKE ?",
                ("alice", f"{MODULE_PREFIX}%"),
            )
        }
        assert set(rows) == {
            f"{MODULE_PREFIX}run_scheduled", f"{MODULE_PREFIX}prune",
        }
        prune = rows[f"{MODULE_PREFIX}prune"]
        assert prune[1] == "17 3 * * *"
        assert json.loads(prune[2]) == ["prune"]
        assert prune[4] == 1
        assert rows[f"{MODULE_PREFIX}run_scheduled"][3] == original_last_run

    def test_seeding_prune_queues_no_immediate_task(self, tmp_path):
        """The first-seed hook fires per newly inserted row, so an upgrade
        that adds only the prune row runs it — and a prune that ran at
        upgrade time would delete on a schedule nobody chose, ahead of the
        daily job. The hook gates on the job name; this is that gate."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        # The prune row was seeded on this sync...
        assert conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}prune"),
        ).fetchone() is not None
        # ...and it queued nothing.
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ?", ("alice",),
        ).fetchone()[0] == 0

    def test_first_seed_of_both_rows_queues_only_the_poll_task(self, tmp_path):
        """A fresh user seeds both rows at once. Exactly one task, and it is
        the poll — asserted on the args rather than on the count alone, since
        a count of one is also what a single stray prune task looks like."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        rows = conn.execute(
            "SELECT skill, skill_args FROM tasks WHERE user_id = ?", ("alice",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "feeds"
        assert json.loads(rows[0][1]) == ["run-scheduled"]

    def test_disabling_the_module_removes_both_rows(self, tmp_path):
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        _sync_feeds_module_jobs(conn, app_config)
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0] == 2
        _sync_feeds_module_jobs(
            conn,
            _make_app_config(
                tmp_path, ["alice"], disabled_modules={"alice": ["feeds"]},
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            ("alice", f"{MODULE_PREFIX}%"),
        ).fetchone()[0] == 0

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
            "WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "feeds"
        assert json.loads(row[2]) == ["run-scheduled"]

    def test_rescues_post_migration_suspended_row(self, tmp_path):
        """Sequence: row was first migrated from command→skill shape,
        then suspended because in-flight tasks created before the
        migration kept hitting the admin gate. Next sync must un-stick
        the row instead of leaving it held back forever."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        # Already-migrated shape (command=NULL, skill set) but suspended,
        # with the admin-gate failure recorded. auto_disabled_at is old
        # enough to clear the 1h cooldown.
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1, 6, ?, "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "command-type tasks are admin-only"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT auto_disabled_at, consecutive_failures, last_error, enabled "
            "FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == 0
        assert row[2] is None
        assert row[3] == 1

    def test_rescues_row_suspended_by_non_admin_gate_failure(self, tmp_path):
        """Wave 2: post-cc0bd54-but-pre-027eb1a, claim_task didn't return
        the skill columns, so module rows fell through to the LLM path
        with an empty prompt and accumulated 5 timeouts / malformed-output
        failures before being suspended. The rescue can't key on a single
        error string for this wave — any suspended module row whose
        auto_disabled_at predates the 1h cooldown gets rescued."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1, 5, ?, "
            "datetime('now', '-9 hours'), datetime('now', '-9 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "Task timed out after 30 minutes"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT auto_disabled_at, consecutive_failures, last_error "
            "FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == 0
        assert row[2] is None

    def test_rescue_skips_recently_suspended_row(self, tmp_path):
        """1h cooldown gate: a row suspended within the last hour stays
        suspended. This caps the rescue→fail→rescue loop rate for genuinely
        broken rows — without it, every */5 cron tick we'd retry 5 more times
        and re-suspend, indefinitely.

        The cooldown reads `auto_disabled_at`, not `last_run_at`: that is the
        timestamp the rule was always about, and the two now differ. This row
        last *ran* nine hours ago and was suspended ten minutes ago, which the
        old predicate would have read as long past its cooldown.
        """
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1, 5, ?, "
            "datetime('now', '-9 hours'), datetime('now', '-10 minutes'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]',
             "Task timed out after 30 minutes"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT auto_disabled_at, consecutive_failures FROM scheduled_jobs "
            "WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == 5

    def test_legacy_command_migration_also_clears_auto_disable(self, tmp_path):
        """One-step migration path: the row is still in command shape AND
        has been stopped by the admin gate. The drift-driven update must do
        both — even when the row was stopped recently (the broad rescue skips
        inside the 1h cooldown, but legacy_command drift fires
        unconditionally).

        Both columns are seeded off, and both have to be cleared: a row this
        old was stopped by code that wrote `enabled = 0`, and nothing else
        will ever turn that back on for a `_module.*` row.
        """
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', ?, NULL, NULL, 0, 1, 6, ?, "
            "datetime('now', '-10 minutes'), datetime('now', '-10 minutes'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "FEEDS_USER=alice istota-skill feeds run-scheduled",
             "command-type tasks are admin-only"),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT command, skill, enabled, consecutive_failures, last_error, "
            "auto_disabled_at "
            "FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] is None
        assert row[1] == "feeds"
        assert row[2] == 1
        assert row[3] == 0
        assert row[4] is None
        assert row[5] is None

    def test_rescue_does_not_touch_operator_paused_row(self, tmp_path):
        """`enabled = 0`, no failures, no suspension: nobody's daemon did this.

        Module rows have no operator-pause UI today, but a direct DB edit or a
        future surface has to survive both arms of the rescue.
        """
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 0, NULL, "
            "datetime('now', '-9 hours'), NULL)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures, auto_disabled_at "
            "FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 0
        assert row[2] is None

    def test_the_rescue_does_not_un_pause_a_suspended_operator_paused_row(
        self, tmp_path,
    ):
        """The suspension lifts; the operator's pause does not.

        The primary arm deliberately does not write `enabled`, which is the
        one thing that would let a retry override somebody's explicit off.
        """
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 5, 'boom', "
            "datetime('now', '-9 hours'), datetime('now', '-9 hours'))",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, auto_disabled_at FROM scheduled_jobs "
            "WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert row[0] == 0, "the rescue must not override an explicit pause"
        assert row[1] is None

    def test_the_legacy_arm_rescues_a_row_the_pre_split_code_stopped(
        self, tmp_path,
    ):
        """The upgrade case, and the one that strands a whole deployment.

        A row the running code auto-disabled is `enabled = 0` with
        `auto_disabled_at` NULL, because the column did not exist when it was
        written, and the migration backfills nothing. Without the legacy arm
        the primary arm cannot see it, no other writer reaches a `_module.*`
        row, and `should_notify` means nobody is told — so every module job
        stopped on the deployment being upgraded is dead for good.
        """
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 5, 'boom', "
            "datetime('now', '-9 hours'), NULL)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures, last_error, auto_disabled_at "
            "FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert tuple(row) == (1, 0, None, None)
        enabled = [j.name for j in db.get_enabled_scheduled_jobs(conn)]
        assert f"{MODULE_PREFIX}run_scheduled" in enabled

    def test_the_legacy_arm_keeps_the_old_cooldown(self, tmp_path):
        """It is today's predicate verbatim, `last_run_at` included, so a row
        stopped inside the last hour waits like it does now."""
        app_config = _make_app_config(tmp_path, ["alice"])
        conn = _conn(tmp_path)
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(user_id, name, cron_expression, prompt, command, skill, "
            "skill_args, enabled, skip_log_channel, consecutive_failures, "
            "last_error, last_run_at, auto_disabled_at) "
            "VALUES (?, ?, ?, '', NULL, ?, ?, 0, 1, 5, 'boom', "
            "datetime('now', '-10 minutes'), NULL)",
            ("alice", f"{MODULE_PREFIX}run_scheduled", "*/5 * * * *",
             "feeds", '["run-scheduled"]'),
        )
        conn.commit()
        _sync_feeds_module_jobs(conn, app_config)
        row = conn.execute(
            "SELECT enabled, consecutive_failures FROM scheduled_jobs "
            "WHERE user_id = ? AND name = ?",
            ("alice", f"{MODULE_PREFIX}run_scheduled"),
        ).fetchone()
        assert tuple(row) == (0, 5)


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
