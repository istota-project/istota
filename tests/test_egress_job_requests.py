"""What a job may ask for before it runs, and the deployment's default for jobs (#910)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from istota import db, scheduler
from istota.config import Config, SecurityConfig, load_config
from istota.cron_loader import (
    CronJob,
    cron_document_errors,
    parse_cron_document,
    render_jobs_block,
    sync_cron_jobs_to_db,
)
from istota.rooms import egress
from istota.rooms.scopes import reach_for_task
from istota.skills._types import SkillMeta

USER = "alice"
SUBJECT = "cron:probe"
INDEX = {
    "browse": SkillMeta(name="browse", description="x", egress="external"),
    "calendar": SkillMeta(name="calendar", description="x", egress="local"),
}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


@pytest.fixture
def conn(path):
    with db.get_db(path) as c:
        yield c


@pytest.fixture
def config(path, tmp_path):
    return Config(db_path=path, temp_dir=tmp_path / "temp")


def _job(**fields) -> CronJob:
    fields.setdefault("name", "probe")
    fields.setdefault("cron", "0 0 1 1 *")
    fields.setdefault("prompt", "check the news")
    return CronJob(**fields)


def _sync(conn, *jobs, **kwargs):
    return sync_cron_jobs_to_db(conn, USER, list(jobs), **kwargs)


def _asked(conn) -> set[str]:
    return {row["entry"] for row in egress.list_refused(conn, USER, SUBJECT)}


def _document(body: str) -> str:
    return f"# Scheduled Jobs\n\n```toml\n[[jobs]]\nname = \"probe\"\ncron = \"0 9 * * *\"\nprompt = \"p\"\n{body}\n```\n"


def test_a_migrated_database_gets_both_columns(tmp_path):
    p = tmp_path / "old.db"
    db.init_db(p)
    with db.get_db(p) as c:
        c.execute("ALTER TABLE tasks DROP COLUMN egress_open_by_default")
        c.execute("ALTER TABLE scheduled_jobs DROP COLUMN egress_request")
        c.commit()
    db.init_db(p)
    with db.get_db(p) as c:
        assert "egress_open_by_default" in {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        assert "egress_request" in {
            r["name"] for r in c.execute("PRAGMA table_info(scheduled_jobs)")
        }


class TestDeclaring:
    def test_a_valid_list_is_kept_in_normal_form_and_survives_a_rewrite(self, config):
        with patch("istota.skills._loader.usable_external_skills", return_value=INDEX):
            doc = parse_cron_document(
                _document('egress = ["News.Example.COM", "skill:browse", "news.example.com"]'),
                config, USER,
            )
        assert doc.jobs[0].egress == ("news.example.com", "skill:browse")
        assert 'egress = ["news.example.com", "skill:browse"]' in render_jobs_block(doc.jobs)
        assert not doc.errors

    @pytest.mark.parametrize("entry", ["open:" + "a" * 64, "public:" + "a" * 64, "10.0.0.1", "nodots"])
    def test_an_entry_no_request_may_hold_is_dropped_and_reported(self, config, entry):
        errors = cron_document_errors(_document(f'egress = ["{entry}"]'), config, USER)
        doc = parse_cron_document(_document(f'egress = ["{entry}"]'), config, USER)
        assert doc.jobs[0].egress == ()
        assert len(errors) == 1 and "egress" in errors[0]

    def test_a_list_that_is_not_one_costs_the_field_and_not_the_job(self, config):
        doc = parse_cron_document(_document('egress = "example.com"'), config, USER)
        assert [job.name for job in doc.jobs] == ["probe"]
        assert doc.jobs[0].egress == ()
        assert doc.errors

    def test_a_skill_the_owner_could_not_approve_is_dropped(self, config):
        """A once job naming one would wait on a button that refuses."""
        usable = {"browse": INDEX["browse"]}
        with patch("istota.skills._loader.usable_external_skills", return_value=usable):
            doc = parse_cron_document(
                _document('egress = ["skill:browse", "skill:calendar", "skill:nonesuch"]'),
                config, USER,
            )
        assert doc.jobs[0].egress == ("skill:browse",)
        assert len(doc.errors) == 2

    def test_the_list_is_bounded_by_what_the_table_keeps_per_subject(self, config):
        hosts = ", ".join(f'"h{i}.example.com"' for i in range(egress.MAX_REFUSED_PER_SUBJECT + 5))
        doc = parse_cron_document(_document(f"egress = [{hosts}]"), config, USER)
        assert len(doc.jobs[0].egress) == egress.MAX_DECLARED
        assert doc.errors

    def test_a_refused_skill_stays_in_the_file_and_asks_for_nothing(self, config):
        with patch("istota.skills._loader.usable_external_skills", return_value={}):
            doc = parse_cron_document(_document('egress = ["skill:browse"]'), config, USER)
        assert doc.jobs[0].egress == ()
        assert 'egress = ["skill:browse"]' in render_jobs_block(doc.jobs)


class TestTheSyncAsks:
    def test_a_declaration_is_a_request_and_grants_nothing(self, conn):
        subjects = _sync(conn, _job(egress=("example.com", "skill:browse")))
        assert subjects == [SUBJECT]
        assert _asked(conn) == {"example.com", "skill:browse"}
        assert egress.grants_for(conn, USER, SUBJECT) == frozenset()
        job = db.get_user_scheduled_jobs(conn, USER)[0]
        task = db.get_task(conn, db.create_task(
            conn, prompt=job.prompt, user_id=USER, source_type="scheduled",
            scheduled_job_id=job.id,
        ))
        reach = reach_for_task(conn, task, skill_index=INDEX)
        assert reach.egress_hosts == frozenset()
        assert "browse" in reach.egress_withheld

    def test_a_later_tick_does_not_ask_again_and_a_dismissal_stands(self, conn):
        _sync(conn, _job(egress=("example.com",)))
        egress.dismiss_refused(conn, USER, SUBJECT, "example.com")
        conn.commit()
        assert _sync(conn, _job(egress=("example.com",))) == []
        row = egress.list_refused(conn, USER, SUBJECT)[0]
        assert row["dismissed"] == 1 and row["hits"] == 1

    def test_an_approved_entry_does_not_ask(self, conn):
        _sync(conn, _job())
        egress.add_grant(conn, USER, SUBJECT, "example.com", decided_via="web")
        conn.commit()
        assert _sync(conn, _job(egress=("example.com",))) == []
        assert _asked(conn) == set()

    def test_withdrawing_a_request_stops_it_asking(self, conn):
        _sync(conn, _job(egress=("example.com", "other.example.com")))
        _sync(conn, _job(egress=("other.example.com",)))
        assert _asked(conn) == {"other.example.com"}

    def test_a_declaration_does_not_touch_the_approved_text(self, conn):
        _sync(conn, _job())
        before = egress.job_digest(egress.job_row_by_subject(conn, USER, SUBJECT))
        _sync(conn, _job(egress=("example.com",)))
        assert egress.job_digest(egress.job_row_by_subject(conn, USER, SUBJECT)) == before

    def test_the_longest_list_beside_a_public_request_settles_and_a_dismissal_stands(self, conn):
        job = _job(
            reach="public",
            egress=tuple(f"h{i}.example.com" for i in range(egress.MAX_DECLARED)),
        )
        _sync(conn, job)
        assert _sync(conn, job) == []
        for row in egress.list_refused(conn, USER, SUBJECT):
            egress.dismiss_refused(conn, USER, SUBJECT, row["entry"])
        conn.commit()
        assert _sync(conn, job) == []
        assert not egress.has_open_refusal(conn, USER, SUBJECT)

    def test_nothing_is_asked_where_the_deployment_needs_no_approval(self, conn):
        assert _sync(conn, _job(egress=("example.com",)), egress_requests=False) == []
        assert _asked(conn) == set()


def _due_once_job(conn, **fields):
    _sync(conn, _job(once=True, **fields))
    # A slot well past `cron_max_staleness_minutes`: approval can take days.
    long_ago = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE scheduled_jobs SET cron_expression = '* * * * *', created_at = ?", (long_ago,),
    )
    conn.commit()


def _tick(conn, config):
    with patch.object(scheduler, "_sync_cron_files"):
        return scheduler.check_scheduled_jobs(conn, config)


class TestAOnceJobWaits:
    def test_it_does_not_fire_until_approved_and_keeps_its_rows(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        assert _tick(conn, config) == []
        job = db.get_user_scheduled_jobs(conn, USER)[0]
        assert job.last_run_at is None, "a stale fire must not spend the slot"
        assert _asked(conn) == {"example.com"}

    def test_it_fires_once_approved_however_late(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        _tick(conn, config)
        egress.add_grant(conn, USER, SUBJECT, "example.com", decided_via="web")
        conn.commit()
        created = _tick(conn, config)
        assert len(created) == 1
        task = db.get_task(conn, created[0])
        assert reach_for_task(conn, task, skill_index=INDEX).egress_hosts == {"example.com"}

    def test_an_approved_public_job_does_not_need_its_hosts(self, conn, config):
        _due_once_job(conn, egress=("example.com",), reach="public")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "public", egress.job_digest(row), decided_via="web")
        conn.commit()
        assert len(_tick(conn, config)) == 1

    def test_an_unapproved_public_once_job_waits_too(self, conn, config):
        _due_once_job(conn, reach="public")
        assert _tick(conn, config) == []

    def test_a_public_once_job_waits_on_that_approval_and_not_on_its_hosts(self, conn):
        _due_once_job(conn, reach="public", egress=("example.com",))
        view = egress.describe_subject(conn, USER, SUBJECT)
        assert view["job"]["waiting"] and view["waiting_on"] == []

    def test_a_dismissal_grants_nothing_so_it_keeps_waiting(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        egress.dismiss_refused(conn, USER, SUBJECT, "example.com")
        conn.commit()
        assert _tick(conn, config) == []
        assert egress.describe_subject(conn, USER, SUBJECT)["waiting_on"] == ["example.com"]

    def test_a_once_job_that_asks_for_nothing_fires_limited(self, conn, config):
        _sync(conn, _job(once=True))
        conn.execute(
            "UPDATE scheduled_jobs SET cron_expression = '* * * * *', "
            "created_at = datetime('now', '-5 minutes')"
        )
        conn.commit()
        assert len(_tick(conn, config)) == 1

    def test_a_recurring_job_runs_with_what_it_has(self, conn, config):
        _sync(conn, _job(egress=("example.com",)))
        conn.execute(
            "UPDATE scheduled_jobs SET cron_expression = '* * * * *', "
            "created_at = datetime('now', '-5 minutes')"
        )
        conn.commit()
        created = _tick(conn, config)
        assert len(created) == 1
        task = db.get_task(conn, created[0])
        assert reach_for_task(conn, task, skill_index=INDEX).egress_hosts == frozenset()


class TestTheDeploymentDefault:
    @pytest.fixture
    def open_config(self, path, tmp_path):
        return Config(
            db_path=path, temp_dir=tmp_path / "temp",
            security=SecurityConfig(scheduled_job_egress="open"),
        )

    def test_the_default_requires_approval(self):
        assert SecurityConfig().scheduled_job_egress == "approval"

    def test_an_unknown_value_reads_as_approval(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[security]\nscheduled_job_egress = "yes please"\n')
        assert load_config(p).security.scheduled_job_egress == "approval"

    def test_open_reads_from_the_file(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[security]\nscheduled_job_egress = "open"\n')
        assert load_config(p).security.scheduled_job_egress == "open"

    def _occurrence(self, conn, cfg, **fields):
        _sync(conn, _job(**fields))
        conn.execute(
            "UPDATE scheduled_jobs SET cron_expression = '* * * * *', "
            "created_at = datetime('now', '-5 minutes')"
        )
        conn.commit()
        created = _tick(conn, cfg)
        assert len(created) == 1
        return db.get_task(conn, created[0])

    def test_open_runs_a_job_unlimited_with_no_grant(self, conn, open_config):
        task = self._occurrence(conn, open_config)
        reach = reach_for_task(conn, task, skill_index=INDEX)
        assert reach.egress_hosts is None
        assert reach.egress_withheld == frozenset()
        assert not reach.public
        # Deliberately what an approved `open:` gets, and what the docs tell the operator.
        assert not reach.isolated_browser

    def test_a_subtask_takes_its_root_answer(self, conn, open_config):
        task = self._occurrence(conn, open_config)
        child = db.get_task(conn, db.create_task(
            conn, prompt="c", user_id=USER, source_type="subtask", parent_task_id=task.id,
        ))
        assert reach_for_task(conn, child, skill_index=INDEX).egress_hosts is None

    def test_a_job_asking_for_public_stays_narrow_until_approved(self, conn, open_config):
        task = self._occurrence(conn, open_config, reach="public")
        reach = reach_for_task(conn, task, skill_index=INDEX)
        assert reach.egress_hosts == frozenset()

    def test_it_covers_jobs_and_nothing_else(self, conn):
        task = db.get_task(conn, db.create_task(
            conn, prompt="p", user_id=USER, source_type="istota_file",
        ))
        assert reach_for_task(conn, task, skill_index=INDEX).egress_hosts == frozenset()

    def test_a_task_queued_under_approval_keeps_that_answer(self, conn, config):
        """The decision is the occurrence's: a config change moves nothing in flight."""
        task = self._occurrence(conn, config)
        assert reach_for_task(conn, task, skill_index=INDEX).egress_hosts == frozenset()

    def test_a_once_job_with_a_request_does_not_wait(self, conn, open_config):
        _due_once_job(conn, egress=("example.com",))
        assert len(_tick(conn, open_config)) == 1

    def test_a_public_once_job_still_waits(self, conn, open_config):
        _due_once_job(conn, reach="public")
        assert _tick(conn, open_config) == []

    def test_doctor_says_so(self, open_config):
        from istota.doctor import WARN, check_egress_open_jobs

        result = check_egress_open_jobs(open_config, False)
        assert result.status == WARN
        assert "scheduled_job_egress" in (result.detail + (result.remedy or ""))

    def test_the_one_view_says_so_and_narrows_for_public(self, conn):
        _sync(conn, _job())
        assert egress.describe_subject(conn, USER, SUBJECT, open_default=True)["open_by_default"]
        assert not egress.describe_subject(conn, USER, SUBJECT)["open_by_default"]
        _sync(conn, _job(reach="public"))
        view = egress.describe_subject(conn, USER, SUBJECT, open_default=True)
        assert not view["open_by_default"]

    def test_the_key_is_a_choice_and_takes_a_typed_confirmation(self):
        from istota.config_editor import schema

        path = ("security", "scheduled_job_egress")
        assert schema.choices_for(path) == ("approval", "open")
        assert path in schema.RISKY_KEYS

