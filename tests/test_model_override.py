"""Per-job model override: CRON.md → ScheduledJob → Task → executor --model flag.

Covers the full chain so a user can pin one cron job to e.g. claude-sonnet-4-6
while everything else uses the default.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig, UserConfig
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
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})

        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled,
                    last_run_at, created_at, model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "feed-digest", "0 0 * * *", "Run feed digest", 1,
                 yesterday, yesterday, "claude-sonnet-4-6"),
            )

        # Fire only if we're past midnight; otherwise the cron won't trigger
        if datetime.now(ZoneInfo("UTC")).hour > 0:
            with db.get_db(db_path) as conn:
                created = check_scheduled_jobs(conn, config)
            assert len(created) == 1
            with db.get_db(db_path) as conn:
                task = db.get_task(conn, created[0])
            assert task.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Executor: task.model overrides config.model
# ---------------------------------------------------------------------------


class TestExecutorModelArg:
    def _make_config(self, tmp_path, model=""):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            model=model,
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
        from istota.commands import cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, model)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "feed-digest", "0 9 * * *", "t", "claude-sonnet-4-6"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)

        assert "claude-sonnet-4-6" in result

    @pytest.mark.asyncio
    async def test_omits_model_when_unset(self, db_path, make_config):
        from istota.commands import cmd_cron

        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "default-job", "0 9 * * *", "t"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)

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
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        user = UserConfig(timezone="UTC")
        config = Config(db_path=db_path, users={"alice": user})
        yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled,
                    last_run_at, created_at, model, effort)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "j", "0 0 * * *", "t", 1,
                 yesterday, yesterday, "claude-sonnet-4-6", "low"),
            )
        if datetime.now(ZoneInfo("UTC")).hour > 0:
            with db.get_db(db_path) as conn:
                created = check_scheduled_jobs(conn, config)
            assert len(created) == 1
            with db.get_db(db_path) as conn:
                task = db.get_task(conn, created[0])
            assert task.model == "claude-sonnet-4-6"
            assert task.effort == "low"


class TestExecutorEffortArg:
    def _make_config(self, tmp_path, model="", effort=""):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            model=model,
            effort=effort,
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
        from istota.commands import cmd_cron
        config = make_config(db_path=db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, effort)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                ("alice", "j", "0 9 * * *", "t", "low"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)
        assert "low" in result


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
        monkeypatch.setattr(scheduler, "edit_talk_message",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("skip")))

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

        try:
            scheduler._finalize_log_channel(
                config, task, "log-room", "**[#42]**",
                FakeCb(), success=True,
                model="claude-sonnet-4-6", effort="low",
            )
        except Exception:
            # Talk send may fail in unit test; we only care about the format call
            pass

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
