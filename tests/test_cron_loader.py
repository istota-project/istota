"""Configuration loading for istota.cron_loader module."""

from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.cron_loader import (
    CronJob,
    generate_cron_md,
    load_cron_jobs,
    migrate_db_jobs_to_file,
    remove_job_from_cron_md,
    sync_cron_jobs_to_db,
    update_job_enabled_in_cron_md,
)
from istota.storage import get_user_cron_path


@pytest.fixture
def mount_path(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    return mount


@pytest.fixture
def make_config_with_mount(tmp_path, mount_path):
    def _make(**overrides):
        db_path = overrides.pop("db_path", tmp_path / "test.db")
        return Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            temp_dir=tmp_path / "temp",
            **overrides,
        )
    return _make


def _write_cron_md(mount_path, user_id, content):
    cron_path = mount_path / get_user_cron_path(user_id, "istota").lstrip("/")
    cron_path.parent.mkdir(parents=True, exist_ok=True)
    cron_path.write_text(content)


# ---------------------------------------------------------------------------
# TestLoadCronJobs
# ---------------------------------------------------------------------------


class TestLoadCronJobs:
    def test_parse_valid_file(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
# Scheduled Jobs

```toml
[[jobs]]
name = "daily-check"
cron = "0 9 * * *"
prompt = "Run daily check"
target = "talk"
room = "room1"

[[jobs]]
name = "weekly"
cron = "0 18 * * 0"
prompt = "Weekly review"
target = "email"
silent_unless_action = true
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 2
        assert jobs[0].name == "daily-check"
        assert jobs[0].cron == "0 9 * * *"
        assert jobs[0].prompt == "Run daily check"
        assert jobs[0].target == "talk"
        assert jobs[0].room == "room1"
        assert jobs[0].enabled is True
        assert jobs[0].silent_unless_action is False

        assert jobs[1].name == "weekly"
        assert jobs[1].target == "email"
        assert jobs[1].silent_unless_action is True

    def test_missing_optional_fields_use_defaults(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
# Jobs

```toml
[[jobs]]
name = "minimal"
cron = "* * * * *"
prompt = "Do stuff"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].target == ""
        assert jobs[0].room == ""
        assert jobs[0].enabled is True
        assert jobs[0].silent_unless_action is False

    def test_enabled_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "paused"
cron = "0 9 * * *"
prompt = "paused job"
enabled = false
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].enabled is False

    def test_no_file_returns_none(self, make_config_with_mount):
        config = make_config_with_mount()
        result = load_cron_jobs(config, "alice")
        assert result is None

    def test_no_mount_returns_none(self, tmp_path):
        config = Config(db_path=tmp_path / "test.db")
        result = load_cron_jobs(config, "alice")
        assert result is None

    def test_empty_toml_block_returns_empty(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
# Scheduled Jobs

```toml
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert jobs == []

    def test_no_toml_block_returns_empty(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", "# Scheduled Jobs\n\nNo config here.\n")
        jobs = load_cron_jobs(config, "alice")
        assert jobs == []

    def test_invalid_toml_returns_none(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs
broken toml
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert jobs is None

    def test_skips_incomplete_jobs(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "no-cron"
prompt = "missing cron"

[[jobs]]
name = "valid"
cron = "0 9 * * *"
prompt = "ok"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "valid"


# ---------------------------------------------------------------------------
# TestGenerateCronMd
# ---------------------------------------------------------------------------


class TestGenerateCronMd:
    def test_basic_generation(self):
        jobs = [
            CronJob(name="daily", cron="0 9 * * *", prompt="Do daily stuff", target="talk", room="room1"),
        ]
        content = generate_cron_md(jobs)
        assert "[[jobs]]" in content
        assert 'name = "daily"' in content
        assert 'cron = "0 9 * * *"' in content
        assert 'target = "talk"' in content
        assert 'room = "room1"' in content

    def test_omits_defaults(self):
        jobs = [CronJob(name="basic", cron="0 * * * *", prompt="test")]
        content = generate_cron_md(jobs)
        assert "target" not in content
        assert "room" not in content
        assert "enabled" not in content
        assert "silent_unless_action" not in content

    def test_includes_disabled(self):
        jobs = [CronJob(name="off", cron="0 * * * *", prompt="test", enabled=False)]
        content = generate_cron_md(jobs)
        assert "enabled = false" in content

    def test_includes_silent(self):
        jobs = [CronJob(name="quiet", cron="0 * * * *", prompt="test", silent_unless_action=True)]
        content = generate_cron_md(jobs)
        assert "silent_unless_action = true" in content

    def test_round_trip(self, mount_path, make_config_with_mount):
        """Generate → write → load should preserve all fields."""
        config = make_config_with_mount()
        original = [
            CronJob(name="j1", cron="0 9 * * *", prompt="first", target="talk", room="r1"),
            CronJob(name="j2", cron="0 18 * * 0", prompt="second", target="email", silent_unless_action=True),
        ]
        content = generate_cron_md(original)
        _write_cron_md(mount_path, "alice", content)
        loaded = load_cron_jobs(config, "alice")
        assert len(loaded) == 2
        assert loaded[0].name == "j1"
        assert loaded[0].target == "talk"
        assert loaded[0].room == "r1"
        assert loaded[1].name == "j2"
        assert loaded[1].silent_unless_action is True

    def test_command_with_inner_quotes(self, mount_path, make_config_with_mount):
        """Commands containing double quotes should round-trip correctly."""
        config = make_config_with_mount()
        original = [CronJob(
            name="email-test",
            cron="0 10 * * *",
            command='python -m istota.skills.email send --subject "Hello World" --body "Test"',
        )]
        content = generate_cron_md(original)
        _write_cron_md(mount_path, "alice", content)
        loaded = load_cron_jobs(config, "alice")
        assert len(loaded) == 1
        assert loaded[0].command == original[0].command

    def test_prompt_with_inner_quotes(self, mount_path, make_config_with_mount):
        """Prompts containing double quotes should round-trip correctly."""
        config = make_config_with_mount()
        original = [CronJob(
            name="quoted-prompt",
            cron="0 10 * * *",
            prompt='Say "hello" to the user',
        )]
        content = generate_cron_md(original)
        _write_cron_md(mount_path, "alice", content)
        loaded = load_cron_jobs(config, "alice")
        assert len(loaded) == 1
        assert loaded[0].prompt == original[0].prompt

    def test_multiple_jobs_separated(self):
        jobs = [
            CronJob(name="a", cron="0 * * * *", prompt="first"),
            CronJob(name="b", cron="0 * * * *", prompt="second"),
        ]
        content = generate_cron_md(jobs)
        # Should have blank line between jobs
        assert "\n\n[[jobs]]" in content


# ---------------------------------------------------------------------------
# TestSyncCronJobsToDb
# ---------------------------------------------------------------------------


class TestSyncCronJobsToDb:
    def test_insert_new_jobs(self, db_path):
        file_jobs = [
            CronJob(name="j1", cron="0 9 * * *", prompt="hello", target="talk", room="r1"),
            CronJob(name="j2", cron="0 18 * * 0", prompt="world", target="email"),
        ]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 2
        assert jobs[0].name == "j1"
        assert jobs[0].cron_expression == "0 9 * * *"
        assert jobs[0].conversation_token == "r1"
        assert jobs[0].output_target == "talk"
        assert jobs[1].name == "j2"
        assert jobs[1].output_target == "email"

    def test_update_existing_job(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "j1", "0 8 * * *", "old prompt"),
            )
        file_jobs = [CronJob(name="j1", cron="0 9 * * *", prompt="new prompt", target="talk", room="r1")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 1
        assert jobs[0].cron_expression == "0 9 * * *"
        assert jobs[0].prompt == "new prompt"
        assert jobs[0].conversation_token == "r1"

    def test_delete_orphaned_jobs(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "orphan", "0 * * * *", "old"),
            )
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", [])
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 0

    def test_preserves_state_fields_when_cron_unchanged(self, db_path):
        """Sync should not overwrite state fields when cron expression hasn't changed."""
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled,
                    last_run_at, consecutive_failures, last_error, last_success_at)
                   VALUES (?, ?, ?, ?, 1, '2026-01-01T00:00:00', 3, 'oops', '2025-12-31T00:00:00')""",
                ("alice", "j1", "0 9 * * *", "old"),
            )
        # Same cron, different prompt — state should be preserved
        file_jobs = [CronJob(name="j1", cron="0 9 * * *", prompt="new")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")

        assert job.last_run_at == "2026-01-01T00:00:00"
        assert job.consecutive_failures == 3
        assert job.last_error == "oops"
        assert job.last_success_at == "2025-12-31T00:00:00"

    def test_resets_last_run_at_on_cron_expression_change(self, db_path):
        """Changing cron expression should reset last_run_at to prevent catch-up runs."""
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled,
                    last_run_at, consecutive_failures, last_error)
                   VALUES (?, ?, ?, ?, 1, '2026-01-01T00:00:00', 3, 'oops')""",
                ("alice", "j1", "0 8 * * *", "old"),
            )
        file_jobs = [CronJob(name="j1", cron="0 9 * * *", prompt="new")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")

        # last_run_at should be reset to now (not the old value)
        assert job.last_run_at != "2026-01-01T00:00:00"
        assert job.last_run_at is not None
        # Other state fields should be preserved
        assert job.consecutive_failures == 3
        assert job.last_error == "oops"

    def test_file_enabled_false_disables_db(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "j1", "0 * * * *", "test"),
            )
        file_jobs = [CronJob(name="j1", cron="0 * * * *", prompt="test", enabled=False)]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")

        assert job.enabled is False

    def test_file_enabled_true_overrides_disabled(self, db_path):
        """File is authoritative: enabled=true in file re-enables a DB-disabled job."""
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 0)""",
                ("alice", "j1", "0 * * * *", "test"),
            )
        file_jobs = [CronJob(name="j1", cron="0 * * * *", prompt="test", enabled=True)]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")

        assert job.enabled is True

    def test_new_job_respects_enabled_false(self, db_path):
        file_jobs = [CronJob(name="new-disabled", cron="0 * * * *", prompt="test", enabled=False)]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "new-disabled")

        assert job.enabled is False

    def test_new_job_starts_enabled(self, db_path):
        file_jobs = [CronJob(name="new-enabled", cron="0 * * * *", prompt="test")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "new-enabled")

        assert job.enabled is True

    def test_does_not_affect_other_users(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("bob", "bob-job", "0 * * * *", "bob stuff"),
            )
        file_jobs = [CronJob(name="alice-job", cron="0 * * * *", prompt="alice stuff")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            alice_jobs = db.get_user_scheduled_jobs(conn, "alice")
            bob_jobs = db.get_user_scheduled_jobs(conn, "bob")

        assert len(alice_jobs) == 1
        assert len(bob_jobs) == 1


# ---------------------------------------------------------------------------
# TestMigrateDbJobsToFile
# ---------------------------------------------------------------------------


class TestMigrateDbJobsToFile:
    def test_creates_file_from_db_jobs(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    output_target, enabled, silent_unless_action)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "daily", "0 9 * * *", "Do stuff", "room1", "talk", 1, 0),
            )
            result = migrate_db_jobs_to_file(conn, config, "alice")

        assert result is True
        cron_path = mount_path / get_user_cron_path("alice", "istota").lstrip("/")
        assert cron_path.exists()
        content = cron_path.read_text()
        assert 'name = "daily"' in content
        assert 'cron = "0 9 * * *"' in content

    def test_does_not_overwrite_existing_file(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        _write_cron_md(mount_path, "alice", "# Existing file\n")
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "j1", "0 * * * *", "test"),
            )
            result = migrate_db_jobs_to_file(conn, config, "alice")

        assert result is False
        content = (mount_path / get_user_cron_path("alice", "istota").lstrip("/")).read_text()
        assert content == "# Existing file\n"

    def test_overwrite_replaces_existing_file(self, db_path, mount_path, make_config_with_mount):
        """overwrite=True writes DB jobs even when file already exists."""
        config = make_config_with_mount(db_path=db_path)
        _write_cron_md(mount_path, "alice", "# Empty template\n")
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "j1", "0 9 * * *", "my job"),
            )
            result = migrate_db_jobs_to_file(conn, config, "alice", overwrite=True)

        assert result is True
        content = (mount_path / get_user_cron_path("alice", "istota").lstrip("/")).read_text()
        assert 'name = "j1"' in content

    def test_no_db_jobs_does_nothing(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            result = migrate_db_jobs_to_file(conn, config, "alice")

        assert result is False
        cron_path = mount_path / get_user_cron_path("alice", "istota").lstrip("/")
        assert not cron_path.exists()

    def test_no_mount_returns_false(self, db_path, tmp_path):
        config = Config(db_path=db_path)
        with db.get_db(db_path) as conn:
            result = migrate_db_jobs_to_file(conn, config, "alice")
        assert result is False

    def test_preserves_disabled_state(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 0)""",
                ("alice", "disabled-job", "0 * * * *", "test"),
            )
            migrate_db_jobs_to_file(conn, config, "alice")

        content = (mount_path / get_user_cron_path("alice", "istota").lstrip("/")).read_text()
        assert "enabled = false" in content

    def test_migrated_file_can_be_loaded(self, db_path, mount_path, make_config_with_mount):
        """Round-trip: DB → file → load should produce valid CronJob list."""
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, conversation_token,
                    output_target, enabled, silent_unless_action)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "j1", "0 9 * * *", "hello world", "room1", "talk", 1, 1),
            )
            migrate_db_jobs_to_file(conn, config, "alice")

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "j1"
        assert jobs[0].cron == "0 9 * * *"
        assert jobs[0].prompt == "hello world"
        assert jobs[0].room == "room1"
        assert jobs[0].target == "talk"
        assert jobs[0].silent_unless_action is True


# ---------------------------------------------------------------------------
# TestCommandJobs
# ---------------------------------------------------------------------------


class TestCommandJobs:
    """Tests for command field support in CronJob."""

    def test_parse_command_job(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "backup"
cron = "0 6 * * *"
command = "python -m istota.skills.memory_search stats"
target = "talk"
room = "room1"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "backup"
        assert jobs[0].command == "python -m istota.skills.memory_search stats"
        assert jobs[0].prompt == ""
        assert jobs[0].target == "talk"
        assert jobs[0].room == "room1"

    def test_reject_both_prompt_and_command(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "bad"
cron = "0 9 * * *"
prompt = "Do stuff"
command = "echo hello"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 0

    def test_reject_neither_prompt_nor_command(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "empty"
cron = "0 9 * * *"
target = "talk"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 0

    def test_generate_command_job(self):
        jobs = [CronJob(name="cmd", cron="0 6 * * *", command="echo hello", target="talk", room="r1")]
        content = generate_cron_md(jobs)
        assert 'command = "echo hello"' in content
        assert "prompt" not in content

    def test_round_trip_command_job(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        original = [
            CronJob(name="prompt-job", cron="0 9 * * *", prompt="Do stuff"),
            CronJob(name="cmd-job", cron="0 6 * * *", command="echo hello", target="talk", room="r1"),
        ]
        content = generate_cron_md(original)
        _write_cron_md(mount_path, "alice", content)
        loaded = load_cron_jobs(config, "alice")
        assert len(loaded) == 2
        assert loaded[0].name == "prompt-job"
        assert loaded[0].prompt == "Do stuff"
        assert loaded[0].command == ""
        assert loaded[1].name == "cmd-job"
        assert loaded[1].command == "echo hello"
        assert loaded[1].prompt == ""

    def test_sync_command_job_insert(self, db_path):
        file_jobs = [
            CronJob(name="cmd1", cron="0 6 * * *", command="echo hi", target="talk", room="r1"),
        ]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 1
        assert jobs[0].name == "cmd1"
        assert jobs[0].command == "echo hi"
        assert jobs[0].prompt == ""

    def test_sync_command_job_update(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, command, enabled)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                ("alice", "cmd1", "0 6 * * *", "", "echo old"),
            )
        file_jobs = [CronJob(name="cmd1", cron="0 7 * * *", command="echo new")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            jobs = db.get_user_scheduled_jobs(conn, "alice")

        assert len(jobs) == 1
        assert jobs[0].command == "echo new"
        assert jobs[0].cron_expression == "0 7 * * *"

    def test_migrate_command_job_to_file(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, command, enabled)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                ("alice", "cmd1", "0 6 * * *", "", "echo hello"),
            )
            migrate_db_jobs_to_file(conn, config, "alice")

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].command == "echo hello"
        assert jobs[0].prompt == ""


# ---------------------------------------------------------------------------
# TestOnceField
# ---------------------------------------------------------------------------


class TestOnceField:
    """Tests for once=true one-time job support."""

    def test_parse_once_from_toml(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "reminder-123"
cron = "30 14 17 2 *"
prompt = "Send reminder"
once = true
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].once is True

    def test_once_defaults_to_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "recurring"
cron = "0 9 * * *"
prompt = "daily check"
```
""")
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].once is False

    def test_once_roundtrips_through_generate(self):
        jobs = [
            CronJob(name="one-shot", cron="0 12 1 3 *", prompt="fire once", once=True),
            CronJob(name="recurring", cron="0 9 * * *", prompt="daily"),
        ]
        content = generate_cron_md(jobs)
        assert "once = true" in content
        # Recurring job should NOT have once line
        lines = content.split("\n")
        # Find the recurring job section — once should only appear once in entire output
        assert content.count("once = true") == 1

    def test_once_synced_to_db(self, db_path):
        file_jobs = [CronJob(name="one-shot", cron="0 12 1 3 *", prompt="fire once", once=True)]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "one-shot")
        assert job.once is True

    def test_once_false_synced_to_db(self, db_path):
        file_jobs = [CronJob(name="recurring", cron="0 9 * * *", prompt="daily")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "recurring")
        assert job.once is False

    def test_once_updated_on_sync(self, db_path):
        """Changing once from false to true in file should update DB."""
        file_jobs = [CronJob(name="j1", cron="0 9 * * *", prompt="test")]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")
            assert job.once is False

        file_jobs = [CronJob(name="j1", cron="0 9 * * *", prompt="test", once=True)]
        with db.get_db(db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", file_jobs)
            job = db.get_scheduled_job_by_name(conn, "alice", "j1")
            assert job.once is True

    def test_migrate_once_job_to_file(self, db_path, mount_path, make_config_with_mount):
        config = make_config_with_mount(db_path=db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, enabled, once)
                   VALUES (?, ?, ?, ?, 1, 1)""",
                ("alice", "reminder-123", "30 14 17 2 *", "Send reminder"),
            )
            migrate_db_jobs_to_file(conn, config, "alice")

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].once is True


# ---------------------------------------------------------------------------
# TestRemoveJobFromCronMd
# ---------------------------------------------------------------------------


class TestRemoveJobFromCronMd:
    """Tests for remove_job_from_cron_md()."""

    def test_removes_target_job(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
# Scheduled Jobs

```toml
[[jobs]]
name = "keep-this"
cron = "0 9 * * *"
prompt = "daily check"

[[jobs]]
name = "remove-me"
cron = "30 14 17 2 *"
prompt = "one-shot"
once = true
```
""")
        result = remove_job_from_cron_md(config, "alice", "remove-me")
        assert result is True

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].name == "keep-this"

    def test_leaves_other_jobs_intact(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "j1"
cron = "0 9 * * *"
prompt = "first"

[[jobs]]
name = "j2"
cron = "0 12 * * *"
prompt = "second"

[[jobs]]
name = "j3"
cron = "0 18 * * *"
prompt = "third"
```
""")
        remove_job_from_cron_md(config, "alice", "j2")

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 2
        assert jobs[0].name == "j1"
        assert jobs[1].name == "j3"

    def test_job_not_found_returns_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "existing"
cron = "0 9 * * *"
prompt = "test"
```
""")
        result = remove_job_from_cron_md(config, "alice", "nonexistent")
        assert result is False

        # Original job should still be there
        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1

    def test_no_cron_file_returns_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        result = remove_job_from_cron_md(config, "alice", "any-job")
        assert result is False

    def test_no_mount_returns_false(self, tmp_path):
        config = Config(db_path=tmp_path / "test.db")
        result = remove_job_from_cron_md(config, "alice", "any-job")
        assert result is False


# ---------------------------------------------------------------------------
# TestUpdateJobEnabledInCronMd
# ---------------------------------------------------------------------------


class TestUpdateJobEnabledInCronMd:
    """Tests for update_job_enabled_in_cron_md()."""

    def test_disable_job_in_file(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "daily-check"
cron = "0 9 * * *"
prompt = "Run daily check"
```
""")
        result = update_job_enabled_in_cron_md(config, "alice", "daily-check", False)
        assert result is True

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].enabled is False

    def test_enable_job_in_file(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "paused"
cron = "0 9 * * *"
prompt = "paused job"
enabled = false
```
""")
        result = update_job_enabled_in_cron_md(config, "alice", "paused", True)
        assert result is True

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 1
        assert jobs[0].enabled is True

    def test_leaves_other_jobs_intact(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "j1"
cron = "0 9 * * *"
prompt = "first"

[[jobs]]
name = "j2"
cron = "0 12 * * *"
prompt = "second"
target = "email"
```
""")
        update_job_enabled_in_cron_md(config, "alice", "j1", False)

        jobs = load_cron_jobs(config, "alice")
        assert len(jobs) == 2
        assert jobs[0].name == "j1"
        assert jobs[0].enabled is False
        assert jobs[1].name == "j2"
        assert jobs[1].enabled is True
        assert jobs[1].target == "email"

    def test_job_not_found_returns_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        _write_cron_md(mount_path, "alice", """\
```toml
[[jobs]]
name = "existing"
cron = "0 9 * * *"
prompt = "test"
```
""")
        result = update_job_enabled_in_cron_md(config, "alice", "nonexistent", False)
        assert result is False

    def test_no_cron_file_returns_false(self, mount_path, make_config_with_mount):
        config = make_config_with_mount()
        result = update_job_enabled_in_cron_md(config, "alice", "any-job", False)
        assert result is False

    def test_no_mount_returns_false(self, tmp_path):
        config = Config(db_path=tmp_path / "test.db")
        result = update_job_enabled_in_cron_md(config, "alice", "any-job", False)
        assert result is False
