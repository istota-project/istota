"""Per-job model override: CRON.md → ScheduledJob → Task → executor --model flag.

Covers the full chain so a user can pin one cron job to e.g. claude-sonnet-4-6
while everything else uses the default.
"""

import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import (
    BrainConfig,
    ClaudeCodeBrainConfig,
    Config,
    NativeBrainConfig,
    SchedulerConfig,
    SecurityConfig,
    UserConfig,
)
from istota.cron_loader import (
    CronJob,
    generate_cron_md,
    load_cron_jobs,
    migrate_db_jobs_to_file,
    sync_cron_jobs_to_db,
)
from istota.scheduler import check_scheduled_jobs
from istota.storage import get_user_cron_path


def _write_cron_md(mount_path, user_id, content):
    cron_path = mount_path / get_user_cron_path(user_id, "istota").lstrip("/")
    cron_path.parent.mkdir(parents=True, exist_ok=True)
    cron_path.write_text(content)


# ---------------------------------------------------------------------------
# CRON.md parsing
# ---------------------------------------------------------------------------


class TestCronLoaderModel:
    def test_parse_model_field(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "feed-digest"
cron = "0 9 * * *"
prompt = "Summarize the feed"
model = "claude-sonnet-4-6"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].model == "claude-sonnet-4-6"

    def test_model_defaults_empty(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "test"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].model == ""

    def test_generate_emits_model(self):
        jobs = [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-sonnet-4-6")]
        out = generate_cron_md(jobs)
        assert 'model = "claude-sonnet-4-6"' in out

    def test_generate_omits_empty_model(self):
        jobs = [CronJob(name="j", cron="0 9 * * *", prompt="t")]
        out = generate_cron_md(jobs)
        assert "model" not in out

    def test_round_trip_model(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        original = [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-sonnet-4-6")]
        _write_cron_md(mount, "alice", generate_cron_md(original))
        loaded = load_cron_jobs(config, "alice")
        assert loaded[0].model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# DB sync (scheduled_jobs.model column)
# ---------------------------------------------------------------------------


class TestSyncModelToDb:
    def test_insert_persists_model(self, db_path):
        file_jobs = [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-sonnet-4-6")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            jobs = db.get_user_scheduled_jobs(conn, "alice")
        assert jobs[0].model == "claude-sonnet-4-6"

    def test_update_changes_model(self, db_path):
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-sonnet-4-6")],
            )
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-opus-4-7")],
            )
            job = db.get_scheduled_job_by_name(conn, "alice", "j")
        assert job.model == "claude-opus-4-7"

    def test_clear_model(self, db_path):
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t", model="claude-sonnet-4-6")],
            )
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t")],
            )
            job = db.get_scheduled_job_by_name(conn, "alice", "j")
        # Empty CronJob.model should null/empty the DB column
        assert (job.model or "") == ""

    def test_migrate_db_to_file_preserves_model(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, model)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "j", "0 9 * * *", "t", "claude-sonnet-4-6"),
            )
            migrate_db_jobs_to_file(conn, config, "alice")

        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Task model column + create_task
# ---------------------------------------------------------------------------


class TestTaskModelColumn:
    def test_create_task_stores_model(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice",
                source_type="scheduled", model="claude-sonnet-4-6",
            )
            task = db.get_task(conn, task_id)
        assert task.model == "claude-sonnet-4-6"

    def test_create_task_default_model_empty(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice", source_type="cli")
            task = db.get_task(conn, task_id)
        assert (task.model or "") == ""


# ---------------------------------------------------------------------------
# Scheduler propagates job.model to task.model
# ---------------------------------------------------------------------------


class TestSchedulerPropagatesModel:
    @patch("istota.scheduler._sync_cron_files")
    def test_job_model_flows_to_task(self, mock_sync, db_path):
        """The unchanged half of the rewritten resolution line: a canonical id
        reaches the row untouched. Was written as `0 0 * * *` behind an
        `if now.hour > 0`, which asserted nothing between midnight and 01:00
        UTC — moved onto `_fire_one_job` so it can fail at any hour.
        """
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        task = _fire_one_job(
            db_path, config, name="feed-digest", prompt="Run feed digest",
            model="claude-sonnet-4-6",
        )
        assert task.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Executor: task.model overrides config.model
# ---------------------------------------------------------------------------


class TestExecutorModelArg:
    def _make_config(self, tmp_path, model=""):
        """The deployment default now lives on the brain that will run the task.

        It used to be the top-level `model`, which the executor substituted into
        every request whatever brain was about to run (ISSUE-418). Passing it
        here rather than at the root is the whole behaviour change these tests
        pin: the default still reaches the argv, by the brain's own `or` instead
        of the executor's substitution.
        """
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            brain=BrainConfig(claude_code=ClaudeCodeBrainConfig(model=model)),
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    @patch("istota.executor.subprocess.run")
    def test_task_model_overrides_config(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, model="claude-opus-4-7")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice",
                source_type="scheduled", model="claude-sonnet-4-6",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        # task.model wins over config.model
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"

    @patch("istota.executor.subprocess.run")
    def test_falls_back_to_config_model(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, model="claude-opus-4-7")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice", source_type="cli")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-4-7"

    @patch("istota.executor.subprocess.run")
    def test_no_model_flag_when_neither_set(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, model="")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice", source_type="cli")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# !cron command shows model
# ---------------------------------------------------------------------------


class TestCmdCronShowsModel:
    @pytest.mark.asyncio
    async def test_lists_model_when_set(self, db_path, make_config):
        from istota.commands import CommandContext, cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, model)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "feed-digest", "0 9 * * *", "t", "claude-sonnet-4-6"),
            )
            AsyncMock()
            result = await cmd_cron(CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args=""))

        assert "claude-sonnet-4-6" in result

    @pytest.mark.asyncio
    async def test_omits_model_when_unset(self, db_path, make_config):
        from istota.commands import CommandContext, cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "default-job", "0 9 * * *", "t"),
            )
            AsyncMock()
            result = await cmd_cron(CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args=""))

        # No "model =" or model token in output for jobs without model override
        assert "claude-" not in result


# ---------------------------------------------------------------------------
# Effort field — same chain as model
# ---------------------------------------------------------------------------


class TestCronLoaderEffort:
    def test_parse_effort_field(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
effort = "low"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].effort == "low"

    def test_generate_emits_effort(self):
        jobs = [CronJob(name="j", cron="0 9 * * *", prompt="t", effort="low")]
        out = generate_cron_md(jobs)
        assert 'effort = "low"' in out

    def test_round_trip_effort(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        original = [CronJob(name="j", cron="0 9 * * *", prompt="t",
                            model="claude-sonnet-4-6", effort="medium")]
        _write_cron_md(mount, "alice", generate_cron_md(original))
        loaded = load_cron_jobs(config, "alice")
        assert loaded[0].model == "claude-sonnet-4-6"
        assert loaded[0].effort == "medium"


class TestSyncEffortToDb:
    def test_insert_persists_effort(self, db_path):
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t", effort="low")],
            )
            jobs = db.get_user_scheduled_jobs(conn, "alice")
        assert jobs[0].effort == "low"

    def test_migrate_db_to_file_preserves_effort(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=db_path, nextcloud_mount_path=mount, temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, effort)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "j", "0 9 * * *", "t", "low"),
            )
            migrate_db_jobs_to_file(conn, config, "alice")
        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].effort == "low"


class TestTaskEffortColumn:
    def test_create_task_stores_effort(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice", source_type="scheduled",
                effort="low",
            )
            task = db.get_task(conn, task_id)
        assert task.effort == "low"


class TestSchedulerPropagatesEffort:
    @patch("istota.scheduler._sync_cron_files")
    def test_job_effort_flows_to_task(self, mock_sync, db_path):
        """Same rewrite as the model case above, and for the same reason."""
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        task = _fire_one_job(
            db_path, config, model="claude-sonnet-4-6", effort="low",
        )
        assert task.model == "claude-sonnet-4-6"
        assert task.effort == "low"


class TestExecutorEffortArg:
    def _make_config(self, tmp_path, model="", effort=""):
        """Per-brain defaults, for the reason above (ISSUE-418)."""
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            brain=BrainConfig(
                claude_code=ClaudeCodeBrainConfig(model=model, effort=effort)
            ),
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    @patch("istota.executor.subprocess.run")
    def test_task_effort_overrides_config(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, effort="high")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice",
                source_type="scheduled", effort="low",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)
        cmd = mock_run.call_args[0][0]
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "low"

    @patch("istota.executor.subprocess.run")
    def test_falls_back_to_config_effort(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, effort="high")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice", source_type="cli")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    @patch("istota.executor.subprocess.run")
    def test_no_effort_when_task_overrides_model_only(self, mock_run, tmp_path):
        """Per-job model override (e.g. Haiku) must not inherit config.effort."""
        config = self._make_config(tmp_path, model="claude-opus-4-7", effort="high")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice",
                source_type="scheduled", model="claude-haiku-4-5",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)
        cmd = mock_run.call_args[0][0]
        assert "--effort" not in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-haiku-4-5"

    @patch("istota.executor.subprocess.run")
    def test_task_overrides_both_model_and_effort(self, mock_run, tmp_path):
        """Explicit per-job effort still applies alongside model override."""
        config = self._make_config(tmp_path, model="claude-opus-4-7", effort="high")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="t", user_id="alice",
                source_type="scheduled",
                model="claude-sonnet-4-6", effort="medium",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "medium"


class TestCmdCronShowsEffort:
    @pytest.mark.asyncio
    async def test_lists_effort_when_set(self, db_path, make_config):
        from istota.commands import CommandContext, cmd_cron
        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, effort)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "j", "0 9 * * *", "t", "low"),
            )
            AsyncMock()
            result = await cmd_cron(CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args=""))
        assert "low" in result


# ---------------------------------------------------------------------------
# Per-job brain pin: CRON.md -> scheduled_jobs.brain, admin-gated
# ---------------------------------------------------------------------------


class TestSyncBrainToDb:
    """The authorization gate, asserted on the stored row.

    ``sync_cron_jobs_to_db`` returns nothing, so the only observable is the
    column. Both legs keep the job — a brain pin is one field of a job that is
    otherwise fine, unlike a non-admin ``command:``, which costs the whole job.
    """

    def test_admin_sync_writes_the_column(self, db_path):
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(
                conn, "alice",
                [CronJob(name="j", cron="0 9 * * *", prompt="t", brain="native")],
                is_admin=True,
            )
            job = db.get_scheduled_job_by_name(conn, "alice", "j")
            # Every reader, not just the one this class is written around.
            # `_row_to_scheduled_job` reads the column defensively
            # (`"brain" in row.keys()`), so a SELECT list that omits it yields
            # `brain=None` silently rather than raising — and
            # `get_enabled_scheduled_jobs` is the one `check_scheduled_jobs`
            # dispatches from, where a dropped column would make every pin a
            # no-op with the suite green.
            enabled = db.get_enabled_scheduled_jobs(conn)
            by_user = db.get_user_scheduled_jobs(conn, "alice")
            by_id = db.get_scheduled_job(conn, job.id)
        assert job is not None, "the job survives an admin sync"
        assert job.brain == "native"
        assert [j.brain for j in enabled] == ["native"]
        assert [j.brain for j in by_user] == ["native"]
        assert by_id.brain == "native"

    def test_non_admin_sync_drops_the_pin_and_warns(self, db_path, caplog):
        """`is_admin=False` explicitly: the kwarg defaults to True, so relying
        on the default would test the wrong thing.
        """
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            with db.get_db(db_path) as conn:
                sync_cron_jobs_to_db(
                    conn, "alice",
                    [CronJob(name="j", cron="0 9 * * *", prompt="t", brain="native")],
                    is_admin=False,
                )
                job = db.get_scheduled_job_by_name(conn, "alice", "j")

        assert job is not None, "the job survives; only the field is dropped"
        assert job.brain is None
        assert any(
            "brain" in r.message and "admin-only" in r.message
            for r in caplog.records
        ), f"expected a dropped-pin warning, got {[r.message for r in caplog.records]}"

    def test_a_non_admin_update_clears_a_pin_already_on_the_row(self, db_path):
        """The gate is not insert-only. The sync rewrites every file-owned
        column on both paths, so a row carrying a pin from an earlier admin
        sync loses it once the author is no longer an admin.
        """
        file_jobs = [CronJob(name="j", cron="0 9 * * *", prompt="t", brain="native")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs, is_admin=True)
            assert db.get_scheduled_job_by_name(conn, "alice", "j").brain == "native"
            sync_cron_jobs_to_db(conn, "alice", file_jobs, is_admin=False)
            job = db.get_scheduled_job_by_name(conn, "alice", "j")
        assert job.brain is None


# ---------------------------------------------------------------------------
# Dispatch: the pin reaches the task row, and the model resolves against the
# brain that will run the job
# ---------------------------------------------------------------------------


def _insert_scheduled_job(conn, **columns):
    """A job that is always due: `* * * * *` with `last_run_at` a day back.

    Deliberately not the `0 0 * * *` + "only assert if the hour is past
    midnight" shape used above — that makes the assertions vacuous for one hour
    a day, which is exactly the failure mode this spec's controls exist to
    catch.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
    row = {
        "user_id": "alice",
        "name": "j",
        "cron_expression": "* * * * *",
        "prompt": "t",
        "enabled": 1,
        "last_run_at": yesterday,
        "created_at": yesterday,
    }
    row.update(columns)
    conn.execute(
        f"INSERT INTO scheduled_jobs ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def _fire_one_job(db_path, config, **columns):
    """Insert one due job, run a dispatch tick, return its task row."""
    with db.get_db(db_path) as conn:
        _insert_scheduled_job(conn, **columns)
    with db.get_db(db_path) as conn:
        created = check_scheduled_jobs(conn, config)
    assert len(created) == 1, f"expected exactly one task, got {created}"
    with db.get_db(db_path) as conn:
        return db.get_task(conn, created[0])


def _dispatch_config(db_path, brain):
    return Config(
        db_path=db_path,
        users={"alice": UserConfig(timezone="UTC")},
        scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
        brain=brain,
    )


# Unmistakable on purpose: a native default that happened to equal the
# anthropic answer would make every namespace assertion below vacuous.
NATIVE_MODEL = "qwen3-testbed-max"


@patch("istota.scheduler._sync_cron_files")
class TestSchedulerPropagatesBrain:
    def test_an_allowlisted_pin_reaches_the_task_row(self, _sync, db_path):
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=NATIVE_MODEL),
            room_selectable=["native"],
        ))
        task = _fire_one_job(db_path, config, brain="native")
        assert task.brain == "native"

    def test_the_column_records_a_pin_the_allowlist_refuses(self, _sync, db_path):
        """Writing the column is not the same as admitting the pin.

        `room_selectable = []` means `resolve_brain_kind` refuses `native` at
        dispatch and the task runs the configured brain — but the row still
        records what the job asked for, which is what `!cron` and the log
        channel read, and what makes shortening the allowlist take effect
        without anything having to rewrite stored rows.

        A refused pin leaves the row saying one kind while the model beside it
        was resolved in another's namespace, which `executor._pin_origin_namespace`
        reads as a crossing and drops. That is the same shape a room with a
        since-dropped pin already has — `commands.brain_for_room` also hands the
        raw pin to `resolve_brain_kind` and returns the resolved config — and the
        function that reconciles them is Stage 4's. Recorded here so the next
        reader meets it at the row rather than in production.
        """
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=NATIVE_MODEL),
            room_selectable=[],
        ))
        task = _fire_one_job(db_path, config, brain="native")
        assert task.brain == "native", (
            "the column records the pin even though resolve_brain_kind refuses "
            "it at dispatch"
        )

    def test_the_model_resolves_against_the_brain_that_runs_not_the_pin(
        self, _sync, db_path
    ):
        """A refused pin does not steer the alias resolution.

        The raw pin and the resolved kind differ only here, and resolving
        against the brain that will actually run is what leaves a job whose kind
        the operator has since dropped from `room_selectable` with a model that
        brain can use.
        """
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=NATIVE_MODEL),
            room_selectable=[],
        ))
        task = _fire_one_job(db_path, config, brain="native", model="smart")
        assert task.model == "claude-opus-5"
        assert task.model != NATIVE_MODEL


@patch("istota.scheduler._sync_cron_files")
class TestSchedulerResolvesTheModelThroughTheJobsBrain:
    """ISSUE-419's two live defects, one per half.

    Both are reachable without a per-job brain: the old line asked
    `make_brain(app_config.brain)`, which is the base kind rather than the one
    the task will run, and `resolve_model_name`, which discards the effort half
    of the pair.
    """

    def test_a_pinned_brain_resolves_the_alias_in_its_own_namespace(
        self, _sync, db_path
    ):
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=NATIVE_MODEL),
            room_selectable=["native"],
        ))
        task = _fire_one_job(db_path, config, brain="native", model="smart")
        assert task.model == NATIVE_MODEL, (
            "a portable tier must resolve in the pinned brain's namespace, not "
            "the deployment default's"
        )

    def test_a_routed_source_type_resolves_the_alias_in_the_routed_namespace(
        self, _sync, db_path
    ):
        """No per-job brain needed to reach the defect.

        `[brain.source_type_overrides] scheduled = "native"` routes the lane, so
        the job runs native and its `smart` must mean native's model. The old
        line stored an anthropic canonical id for a task that would run native.

        What this asserts is the *stored* value, and only that. Until Stage 4
        teaches `_pin_origin_namespace` about `source_type_overrides` the
        executor still reads the base kind's namespace off a NULL `tasks.brain`,
        sees a crossing and drops the pin — so on this lane the commit changes
        which name is dropped rather than that one is. The spec says so under
        Behaviour, and it is not a fix to read into this assertion.
        """
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=NATIVE_MODEL),
            source_type_overrides={"scheduled": "native"},
        ))
        task = _fire_one_job(db_path, config, model="smart")
        assert task.brain is None, "no per-job pin in this case"
        assert task.model == NATIVE_MODEL

    def test_an_effort_bearing_model_reference_carries_its_effort(
        self, _sync, db_path
    ):
        """Not built on a bare tier: the shipped `DEFAULT_ALIASES` all carry
        `None`, so `smart` would pass under either resolver and assert nothing.
        `opus:high` is the `:effort` modifier `_validate_model`'s docstring
        advertises and `docs/features/scheduling.md` lets an operator write.
        """
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        task = _fire_one_job(db_path, config, model="opus:high")
        assert task.model == "claude-opus-5"
        assert task.effort == "high", (
            "resolve_model_name discards the effort half of the pair; "
            "resolve_alias keeps it"
        )

    def test_the_jobs_own_effort_wins_over_the_alias(self, _sync, db_path):
        """`with_defaults`' precedence rule with the job row standing in for
        the block: the job wrote both, so it keeps both.

        The corollary the review asked to have stated: an operator writing
        `haiku:high` now gets `high` on the row where it used to be discarded
        with the rest of the pair. That is the intended direction —
        `_resolve_effort` protects against an effort *inherited* from a default
        chosen for another model, not against one written on this line — and it
        is what makes cron agree with `!room model`.
        """
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        task = _fire_one_job(db_path, config, model="opus:high", effort="low")
        assert task.model == "claude-opus-5"
        assert task.effort == "low"

    def test_an_alias_that_resolves_only_an_effort_still_carries_it(
        self, _sync, db_path
    ):
        """`resolve_alias` can answer `(None, "high")`.

        A built-in role on a native brain with no configured model returns the
        pair with a falsy model and a live effort (`NativeBrain.resolve_alias`),
        so reading the effort inside the model branch loses the modifier in
        exactly the case the operator wrote one. `resolve_model_name` then
        collapses the role to the empty configured model, which is the
        documented "brain default" — the effort is the only half left to keep.
        """
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model=""),
            room_selectable=["native"],
        ))
        task = _fire_one_job(db_path, config, brain="native", model="general:high")
        assert task.model is None, "an unset native model means brain default"
        assert task.effort == "high"

    def test_a_name_no_brain_resolves_is_used_as_written_and_warns(
        self, _sync, db_path, caplog
    ):
        """The third thing this stage's spec asks the call site for.

        Not an error — a brain that does no aliasing passes an explicit id
        through by design — so the row keeps the name and the line says what
        will happen. The `:effort` half is dropped here, matching
        `_resolve_crossing_model_effort`'s fallback, and saying so is the whole
        reason it warns.
        """
        from istota import scheduler

        scheduler._warned_keys.clear()
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            task = _fire_one_job(db_path, config, model="gpt-5-turbo:high")
        assert task.model == "gpt-5-turbo"
        assert task.effort is None, "the modifier is dropped on this path"
        assert any(
            "gpt-5-turbo:high" in r.message and "dropping" in r.message
            for r in caplog.records
        ), f"expected one warning, got {[r.message for r in caplog.records]}"

    def test_a_brain_that_cannot_be_constructed_costs_only_its_own_resolution(
        self, _sync, db_path, caplog
    ):
        """`make_brain` is not confined to `ValueError`.

        A malformed `[brain.native] base_url` reaches `httpx.InvalidURL`, which
        derives from `Exception` directly, so the enclosing `except ValueError`
        around `create_task` would not catch it — and `get_db` skips its commit
        on an exception, so one such row would roll back every job already
        queued in the tick. The job still runs, with the name passed through for
        the executor to resolve again.
        """
        config = _dispatch_config(db_path, BrainConfig(
            kind="claude_code",
            # An unclosed IPv6 bracket: `httpx.AsyncClient.__init__` raises
            # `InvalidURL` on it eagerly. Most malformed URLs do not — they are
            # accepted here and fail at request time — so this is the shape that
            # actually reaches the constructor.
            native=NativeBrainConfig(model="x", base_url="http://[::1"),
            room_selectable=["native"],
        ))
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            task = _fire_one_job(db_path, config, brain="native", model="smart")
        assert task.model == "smart", "passed through unresolved, not dropped"
        assert task.brain == "native"
        assert any("could not resolve model" in r.message for r in caplog.records)

    def test_a_non_string_pin_does_not_take_the_tick_down(self, _sync, db_path):
        """`resolve_brain_kind` opens with `(override or "").strip()`.

        That raises `AttributeError` on a non-string, from a call that looks
        total — outside the `except ValueError` around `create_task`, so it
        would cost every job after it in the same tick rather than its own row.
        `commands.brain_for_room` coerces the sibling column for the same
        reason, and its docstring records the same hazard.

        The value is injected on the row object rather than through the column,
        because it cannot be reached through the column: `scheduled_jobs.brain`
        has TEXT affinity, which converts an integer or a float to a string on
        the way in, and the one type that survives unconverted — a BLOB — has
        `.strip()`. So this pins the guard at the boundary the function actually
        has, which is the dataclass any other writer of it could fill.
        """
        config = _dispatch_config(db_path, BrainConfig(kind="claude_code"))
        with db.get_db(db_path) as conn:
            _insert_scheduled_job(conn, name="a", model="opus")
            _insert_scheduled_job(conn, name="b", model="opus")
            rows = db.get_enabled_scheduled_jobs(conn)
        bad = [
            r if r.name != "a" else dataclasses.replace(r, brain=5)
            for r in rows
        ]
        with patch("istota.scheduler.db.get_enabled_scheduled_jobs", return_value=bad):
            with db.get_db(db_path) as conn:
                created = check_scheduled_jobs(conn, config)
        assert len(created) == 2, "the sibling job is not collateral"
        with db.get_db(db_path) as conn:
            tasks = [db.get_task(conn, t) for t in created]
        assert sorted(t.model for t in tasks) == ["claude-opus-5", "claude-opus-5"]
        assert sorted((t.brain or "") for t in tasks) == ["", "5"]


class TestCmdCronShowsBrain:
    """`!cron` renders the pin, on `TestCmdCronShowsModel`'s pattern.

    The listing is what makes a sync-time gate honest: a value dropped for a
    non-admin leaves the column NULL, so this line shows what will run rather
    than what the file says.
    """

    @pytest.mark.asyncio
    async def test_lists_brain_when_set(self, db_path, make_config):
        from istota.commands import CommandContext, cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, brain)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "nightly", "0 9 * * *", "t", "native"),
            )
            result = await cmd_cron(CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args=""))

        assert "brain: native" in result

    @pytest.mark.asyncio
    async def test_omits_brain_when_unset(self, db_path, make_config):
        from istota.commands import CommandContext, cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "default-job", "0 9 * * *", "t"),
            )
            result = await cmd_cron(CommandContext(
                config=config, conn=conn, user_id="alice",
                conversation_token="room1", args=""))

        assert "brain" not in result


# ---------------------------------------------------------------------------
# Validation warnings on CRON.md load
# ---------------------------------------------------------------------------


class TestLogChannelShowsResolvedModelEffort:
    """Resolved model/effort is surfaced in log channel finalize call."""

    def test_format_uses_task_override_over_config(self):
        from istota.scheduler import _format_log_channel_body
        # Caller resolves: task override wins; we just verify format passes through.
        body = _format_log_channel_body(
            ("**[#42]**", "Dev"), ["x"],
            done=True, success=True,
            model="claude-sonnet-4-6", effort="low",
        )
        assert "(claude-sonnet-4-6 low)" in body

    def test_finalize_log_channel_passes_resolved_values(self, tmp_path, monkeypatch):
        """_finalize_log_channel called from process_one_task should resolve task → config."""
        from istota import scheduler

        captured = {}

        def fake_format(prefix, descriptions, **kwargs):
            captured.update(kwargs)
            return "stub"

        monkeypatch.setattr(scheduler, "_format_log_channel_body", fake_format)

        config = Config(
            db_path=tmp_path / "x.db", model="claude-opus-4-7", effort="high",
        )
        task = db.Task(
            id=42, status="completed", source_type="scheduled",
            user_id="alice", prompt="t",
            model="claude-sonnet-4-6", effort="low",
        )

        # Stub log_callback with the minimal attrs _finalize_log_channel reads
        class FakeCb:
            all_descriptions = []
            log_msg_id = [None]

        # No destinations: the body is composed before the delivery loop, so
        # this is the whole of what the test claims and there is nothing left
        # to swallow. The previous version passed the string "log-room" where a
        # `list[Destination]` belongs, so the loop raised `AttributeError` on
        # the first character and a bare `except Exception` caught it — which
        # is also why no room-shape conversion applies here: the Talk seam was
        # never reached, and could not be.
        scheduler._finalize_log_channel(
            config, task, [], "**[#42]**",
            FakeCb(), success=True,
            model="claude-sonnet-4-6", effort="low",
        )

        # Resolved values reached the formatter
        assert captured.get("model") == "claude-sonnet-4-6"
        assert captured.get("effort") == "low"


class TestModelEffortValidation:
    def test_warns_on_suspicious_model(self, tmp_path, caplog):
        import logging
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        # Missing "claude-" prefix — likely a typo
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
model = "sonnet-4-6"
```
""")
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            jobs = load_cron_jobs(config, "alice")
        # Job is still loaded (warn, don't reject)
        assert len(jobs) == 1
        assert jobs[0].model == "sonnet-4-6"
        assert any("model" in r.getMessage().lower() for r in caplog.records)

    def test_warns_on_whitespace_in_model(self, tmp_path, caplog):
        import logging
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
model = "claude sonnet 4-6"
```
""")
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert any("model" in r.getMessage().lower() for r in caplog.records)

    def test_warns_on_unknown_effort(self, tmp_path, caplog):
        import logging
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
effort = "extreme"
```
""")
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            jobs = load_cron_jobs(config, "alice")
        # Loaded but warns
        assert len(jobs) == 1
        assert jobs[0].effort == "extreme"
        assert any("effort" in r.getMessage().lower() for r in caplog.records)

    def test_no_warning_for_known_values(self, tmp_path, caplog):
        import logging
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
model = "claude-sonnet-4-6"
effort = "low"
```
""")
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        # No warnings about model or effort
        msgs = [r.getMessage().lower() for r in caplog.records]
        assert not any("model" in m or "effort" in m for m in msgs)

    def test_no_warning_when_unset(self, tmp_path, caplog):
        import logging
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        _write_cron_md(mount, "alice", """\
```toml
[[jobs]]
name = "j"
cron = "0 9 * * *"
prompt = "t"
```
""")
        with caplog.at_level(logging.WARNING, logger="istota.cron_loader"):
            jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        msgs = [r.getMessage().lower() for r in caplog.records]
        assert not any("model" in m or "effort" in m for m in msgs)
