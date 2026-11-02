"""A `once` job edited while an occurrence is in flight keeps the edit (#920)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from istota import cron_loader, db, scheduler
from istota.config import Config, EmailConfig, NextcloudConfig, TalkConfig
from istota.rooms import egress
from istota.storage import get_user_cron_path

USER = "alice"
NAME = "reminder"
SUBJECT = egress.cron_subject(NAME)
OLD = "the old text"
NEW = "the new text"


@pytest.fixture
def config(db_path, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(url="https://nc.example.com", username="istota", app_password="s"),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
    )


def _write(config, *, prompt=OLD, cron="0 15 20 2 *", extra=""):
    path = config.workspace_path / get_user_cron_path(USER, "istota").lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Scheduled Jobs\n\n```toml\n[[jobs]]\n"
        f'name = "{NAME}"\ncron = "{cron}"\nprompt = "{prompt}"\nonce = true\n{extra}```\n'
    )
    return path


def _sync(config):
    doc = cron_loader.load_cron_document(config, USER)
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, USER, doc.jobs, unread_names=doc.unread_names)


def _queue(config):
    with db.get_db(config.db_path) as conn:
        job = db.get_scheduled_job_by_name(conn, USER, NAME)
        scheduler._create_scheduled_job_task(conn, config, job)
        db.set_scheduled_job_last_run(conn, job.id)
    return job.id


def _run(config, during=lambda: None):
    def execute(*args, **kwargs):
        during()
        return True, "done", None, None

    with (
        patch("istota.scheduler.execute_task", side_effect=execute),
        patch("istota.scheduler.asyncio.run", return_value=42),
    ):
        return scheduler.process_one_task(config)


def _row(config):
    with db.get_db(config.db_path) as conn:
        return db.get_scheduled_job_by_name(conn, USER, NAME)


def _file_prompts(config):
    return [job.prompt for job in cron_loader.load_cron_jobs(config, USER)]


class TestAnEditTheSyncHasRead:
    def test_the_old_occurrence_does_not_delete_the_new_text(self, config):
        _write(config)
        _sync(config)
        job_id = _queue(config)

        def edit():
            _write(config, prompt=NEW)
            _sync(config)

        _run(config, edit)

        row = _row(config)
        assert row is not None and row.id == job_id and row.prompt == NEW
        assert _file_prompts(config) == [NEW]

    def test_the_approval_request_the_edit_raised_survives(self, config):
        """The sequence as filed: an approved public job edited while it is queued."""
        extra = 'reach = "public"\n'
        _write(config, extra=extra)
        _sync(config)
        with db.get_db(config.db_path) as conn:
            egress.approve_job(conn, USER, SUBJECT, "public", None, decided_via="cli")
        _queue(config)
        _write(config, prompt=NEW, extra=extra)
        _sync(config)

        _run(config)

        with db.get_db(config.db_path) as conn:
            assert egress.once_job_waiting(conn, USER, NAME, ceiling="limited")
            assert "public" in {row["entry"] for row in egress.list_refused(conn, USER, SUBJECT)}
        assert _file_prompts(config) == [NEW]

    def test_a_dated_job_fires_the_new_text_rather_than_next_year(self, config):
        now = datetime.now(UTC)
        slot = now - timedelta(minutes=10)
        _write(config, cron=f"{slot.minute} {slot.hour} {slot.day} {slot.month} *")
        _sync(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET created_at = ?",
                ((now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),),
            )
        _queue(config)

        def edit():
            _write(config, prompt=NEW, cron=f"{slot.minute} {slot.hour} {slot.day} {slot.month} *")
            _sync(config)

        _run(config, edit)

        with db.get_db(config.db_path) as conn, patch.object(scheduler, "_sync_cron_files"):
            created = scheduler.check_scheduled_jobs(conn, config)
            assert len(created) == 1
            assert db.get_task(conn, created[0]).prompt == NEW

        _run(config)
        assert _row(config) is None
        assert _file_prompts(config) == []


class TestAnEditNoSyncHasRead:
    def test_the_file_keeps_the_edit_and_the_next_sync_takes_it(self, config):
        _write(config)
        _sync(config)
        job_id = _queue(config)

        _run(config, lambda: _write(config, prompt=NEW))

        assert _file_prompts(config) == [NEW]
        assert _row(config).id == job_id
        _sync(config)
        row = _row(config)
        assert row.id == job_id and row.prompt == NEW and row.last_run_at is None

    def test_a_grant_kept_for_the_new_text_on_the_page_survives(self, config):
        extra = 'reach = "public"\n'
        _write(config, extra=extra)
        _sync(config)
        with db.get_db(config.db_path) as conn:
            egress.approve_job(conn, USER, SUBJECT, "public", None, decided_via="cli")
        _queue(config)

        def save_on_the_page():
            _write(config, prompt=NEW, extra=extra)
            job = cron_loader.load_cron_jobs(config, USER)[0]
            digest = cron_loader.job_text_digest(job, is_admin=True)
            with db.get_db(config.db_path) as conn:
                egress.approve_saved_text(conn, USER, SUBJECT, "public", digest, decided_via="web")

        _run(config, save_on_the_page)
        _sync(config)

        with db.get_db(config.db_path) as conn:
            assert not egress.once_job_waiting(conn, USER, NAME, ceiling="limited")

    def test_an_edit_that_does_not_load_stays_in_the_file_and_cannot_repeat(self, config):
        """The row goes: as the file's only job a sync would hold it unchanged, and
        a recurring schedule would then fire the finished text at every slot."""
        _write(config, cron="*/5 * * * *")
        _sync(config)
        _queue(config)
        path = config.workspace_path / get_user_cron_path(USER, "istota").lstrip("/")

        def typo():
            path.write_text(
                "# Scheduled Jobs\n\n```toml\n[[jobs]]\n"
                f'name = "{NAME}"\nprompt = "{NEW}"\nonce = true\n```\n'
            )

        _run(config, typo)

        assert NEW in path.read_text()
        assert _row(config) is None

    def test_a_file_that_will_not_parse_keeps_the_edit(self, config):
        _write(config)
        _sync(config)
        _queue(config)
        path = config.workspace_path / get_user_cron_path(USER, "istota").lstrip("/")
        broken = f'# Scheduled Jobs\n\n```toml\n[[jobs]]\nname = "{NAME}"\nprompt = "{NEW}\n```\n'

        _run(config, lambda: path.write_text(broken))

        assert path.read_text() == broken
        assert _row(config) is None

    def test_turning_once_off_keeps_the_job(self, config):
        _write(config)
        _sync(config)
        job_id = _queue(config)
        path = config.workspace_path / get_user_cron_path(USER, "istota").lstrip("/")

        def recurring():
            path.write_text(
                f'# Scheduled Jobs\n\n```toml\n[[jobs]]\nname = "{NAME}"\n'
                f'cron = "0 9 * * *"\nprompt = "{OLD}"\n```\n'
            )

        _run(config, recurring)

        assert _file_prompts(config) == [OLD]
        assert _row(config).id == job_id
        _sync(config)
        assert _row(config).once == 0


class TestWhatStillGoes:
    def test_an_unedited_job_goes_from_both(self, config):
        _write(config)
        _sync(config)
        _queue(config)

        _run(config)

        assert _row(config) is None
        assert _file_prompts(config) == []

    def test_a_schedule_edit_is_not_an_edit_of_what_ran(self, config):
        _write(config)
        _sync(config)
        _queue(config)

        def move():
            _write(config, cron="0 16 20 2 *")
            _sync(config)

        _run(config, move)

        assert _row(config) is None
        assert _file_prompts(config) == []

    def test_a_prompt_file_that_will_not_read_is_not_an_edit(self, config, monkeypatch):
        _write(config)
        real = cron_loader.load_cron_document

        def unread(cfg, user_id):
            doc = real(cfg, user_id)
            doc.jobs = []
            doc.unread_names = frozenset({NAME})
            return doc

        monkeypatch.setattr(cron_loader, "load_cron_document", unread)
        outcome = cron_loader.remove_finished_once_job(config, USER, NAME, "not-the-digest")
        assert outcome == cron_loader.ONCE_REMOVED


class TestTheTick:
    def _slot_ago(self, config, minutes):
        now = datetime.now(UTC)
        slot = now - timedelta(minutes=minutes)
        _write(config, cron=f"{slot.minute} {slot.hour} {slot.day} {slot.month} *")
        _sync(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET created_at = ?",
                ((slot - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),),
            )

    def _tick(self, config):
        with db.get_db(config.db_path) as conn, patch.object(scheduler, "_sync_cron_files"):
            return scheduler.check_scheduled_jobs(conn, config)

    def test_a_kept_job_whose_edit_came_long_after_its_slot_still_fires(self, config):
        self._slot_ago(config, 70)
        _queue(config)
        cron = cron_loader.load_cron_jobs(config, USER)[0].cron

        def edit():
            _write(config, prompt=NEW, cron=cron)
            _sync(config)

        _run(config, edit)

        assert len(self._tick(config)) == 1

    def test_a_once_job_that_never_ran_is_not_spent_as_stale(self, config):
        self._slot_ago(config, 70)
        assert len(self._tick(config)) == 1

    def test_a_once_job_that_ran_is_still_held_to_staleness(self, config):
        self._slot_ago(config, 70)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET last_run_at = ?",
                ((datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S"),),
            )
        assert self._tick(config) == []

    def test_a_slot_that_passed_during_the_run_does_not_fire_before_the_flush(self, config):
        _write(config, cron="*/5 * * * *")
        _sync(config)
        _queue(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET last_run_at = ?",
                ((datetime.now(UTC) - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"),),
            )
        fired = []

        with patch(
            "istota.scheduler.deliver_pending",
            side_effect=lambda *a, **k: fired.extend(self._tick(config)),
        ):
            _run(config)

        assert fired == []
        assert _row(config) is None


class TestTheSyncsReset:
    def _prior_run(self, config):
        with db.get_db(config.db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, USER, NAME)
            db.set_scheduled_job_last_run(conn, job.id)

    def test_a_text_edit_clears_the_spent_slot(self, config):
        _write(config)
        _sync(config)
        self._prior_run(config)
        _write(config, prompt=NEW)
        _sync(config)
        assert _row(config).last_run_at is None

    def test_a_schedule_edit_alongside_keeps_the_no_catch_up_reset(self, config):
        _write(config)
        _sync(config)
        self._prior_run(config)
        _write(config, prompt=NEW, cron="0 16 20 2 *")
        _sync(config)
        assert _row(config).last_run_at is not None

    def test_a_recurring_job_keeps_its_slot(self, config):
        path = config.workspace_path / get_user_cron_path(USER, "istota").lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        body = '# Scheduled Jobs\n\n```toml\n[[jobs]]\nname = "{}"\ncron = "0 9 * * *"\nprompt = "{}"\n```\n'
        path.write_text(body.format(NAME, OLD))
        _sync(config)
        self._prior_run(config)
        path.write_text(body.format(NAME, NEW))
        _sync(config)
        assert _row(config).last_run_at is not None
