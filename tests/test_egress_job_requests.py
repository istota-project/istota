"""What a job may ask for before it runs, and the deployment's default for jobs (#910)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from istota import db, scheduler
from istota.commands import retry_task
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
        c.execute("ALTER TABLE tasks DROP COLUMN egress_unapproved")
        c.execute("ALTER TABLE scheduled_jobs DROP COLUMN egress_request")
        c.commit()
    db.init_db(p)
    with db.get_db(p) as c:
        assert "egress_unapproved" in {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
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
        assert _sync(conn, _job(egress=("example.com",)), ceiling="full") == []
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

    def test_an_unapproved_public_once_job_waits_too(self, conn, path, tmp_path):
        _due_once_job(conn, reach="public")
        assert _tick(conn, _config_at(path, tmp_path, "limited")) == []

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


def _config_at(path, tmp_path, ceiling):
    return Config(
        db_path=path, temp_dir=tmp_path / "temp",
        security=SecurityConfig(scheduled_job_max_unapproved=ceiling),
    )


def _occurrence(conn, cfg, **fields):
    _sync(conn, _job(**fields), ceiling=cfg.security.scheduled_job_max_unapproved)
    conn.execute(
        "UPDATE scheduled_jobs SET cron_expression = '* * * * *', "
        "created_at = datetime('now', '-5 minutes')"
    )
    conn.commit()
    created = _tick(conn, cfg)
    assert len(created) == 1
    return db.get_task(conn, created[0])


class TestAOnceJobRunByHand:
    """`run_scheduled_job_now` holds where the tick holds (#918): a manual run would
    otherwise run limited, delete the job and take the request with it."""

    def _run(self, conn, config):
        job = db.get_user_scheduled_jobs(conn, USER)[0]
        return scheduler.run_scheduled_job_now(conn, config, job)

    def test_it_is_refused_while_waiting_and_keeps_its_rows(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        task_id, refusal = self._run(conn, config)
        assert task_id is None
        assert "waiting for you to approve" in refusal
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert len(db.get_user_scheduled_jobs(conn, USER)) == 1
        assert _asked(conn) == {"example.com"}

    def test_it_uses_the_deployments_ceiling(self, conn, path, tmp_path):
        _due_once_job(conn, reach="public")
        task_id, refusal = self._run(conn, _config_at(path, tmp_path, "limited"))
        assert task_id is None and "waiting for you to approve" in refusal
        task_id, refusal = self._run(conn, _config_at(path, tmp_path, "public"))
        assert task_id is not None and refusal == ""

    def test_it_runs_once_approved(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        egress.add_grant(conn, USER, SUBJECT, "example.com", decided_via="web")
        conn.commit()
        task_id, refusal = self._run(conn, config)
        assert task_id is not None and refusal == ""

    def test_a_once_job_that_asks_for_nothing_still_runs(self, conn, config):
        _due_once_job(conn)
        task_id, refusal = self._run(conn, config)
        assert task_id is not None and refusal == ""

    def test_a_recurring_job_with_an_open_request_still_runs(self, conn, config):
        _sync(conn, _job(egress=("example.com",)))
        task_id, refusal = self._run(conn, config)
        assert task_id is not None and refusal == ""

    def test_a_disabled_job_is_refused_though_the_page_does_not_show_it_waiting(self, conn, config):
        # A successful run would still delete it, so the hold outlasts the tick's interest.
        _due_once_job(conn, egress=("example.com",))
        db.disable_scheduled_job(conn, db.get_user_scheduled_jobs(conn, USER)[0].id)
        assert not egress.describe_subject(conn, USER, SUBJECT)["job"]["waiting"]
        task_id, refusal = self._run(conn, config)
        assert task_id is None and "waiting for you to approve" in refusal

    def test_retry_on_an_earlier_occurrence_is_refused_too(self, conn, config):
        _due_once_job(conn, egress=("example.com",))
        job = db.get_user_scheduled_jobs(conn, USER)[0]
        old = db.create_task(
            conn, prompt="check the news", user_id=USER, source_type="scheduled",
            scheduled_job_id=job.id, queue="background",
        )
        db.update_task_status(conn, old, "failed", error="attempts exhausted")
        out = retry_task(conn, config, USER, db.get_task(conn, old))
        assert isinstance(out, str) and "waiting for you to approve" in out
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?", (old,),
        ).fetchone()[0] == 0


class TestTheReachField:
    @pytest.mark.parametrize("value,stored", [
        ("limited", "limited"), ("public", "public"), ("full", "full"),
        # The earlier spelling of limited.
        ("private", "limited"),
        # A typo must not read as a request for more.
        ("everything", "limited"),
    ])
    def test_a_written_value(self, config, value, stored):
        doc = parse_cron_document(_document(f'reach = "{value}"'), config, USER)
        assert doc.jobs[0].reach == stored

    def test_no_value_is_no_value(self, config):
        doc = parse_cron_document(_document(""), config, USER)
        assert doc.jobs[0].reach == ""
        assert "reach" not in render_jobs_block(doc.jobs)

    def test_a_typo_is_reported(self, config):
        assert parse_cron_document(_document('reach = "everything"'), config, USER).errors

    def test_full_survives_a_rewrite(self, config):
        doc = parse_cron_document(_document('reach = "full"'), config, USER)
        assert 'reach = "full"' in render_jobs_block(doc.jobs)


class TestTheSetting:
    def test_the_default_is_public(self):
        assert SecurityConfig().scheduled_job_max_unapproved == "public"

    def test_a_typo_reads_as_the_narrowest(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[security]\nscheduled_job_max_unapproved = "yes please"\n')
        assert load_config(p).security.scheduled_job_max_unapproved == "limited"

    @pytest.mark.parametrize("value", ["limited", "public", "full"])
    def test_it_reads_from_the_file(self, tmp_path, value):
        p = tmp_path / "config.toml"
        p.write_text(f'[security]\nscheduled_job_max_unapproved = "{value}"\n')
        assert load_config(p).security.scheduled_job_max_unapproved == value

    def test_the_key_is_a_choice_and_takes_a_typed_confirmation(self):
        from istota.config_editor import schema

        path = ("security", "scheduled_job_max_unapproved")
        assert schema.choices_for(path) == ("limited", "public", "full")
        assert path in schema.RISKY_KEYS

    @pytest.mark.parametrize("reach,ceiling,expected", [
        ("private", "limited", "limited"), ("private", "public", "limited"),
        ("private", "full", "full"),
        ("limited", "limited", "limited"), ("limited", "public", "limited"),
        ("limited", "full", "limited"),
        ("public", "limited", "limited"), ("public", "public", "public"),
        ("public", "full", "public"),
        ("full", "limited", "limited"), ("full", "public", "limited"),
        ("full", "full", "full"),
    ])
    def test_what_a_job_gets_with_no_approval(self, reach, ceiling, expected):
        assert egress.unapproved_reach(reach, ceiling) == expected


class TestWhatAnOccurrenceRunsAs:
    def test_public_under_public_needs_no_approval(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "public")
        reach = reach_for_task(conn, _occurrence(conn, cfg, reach="public"), skill_index=INDEX)
        assert reach.public and reach.egress_hosts is None

    def test_a_subtask_of_a_public_occurrence_is_public(self, conn, path, tmp_path):
        task = _occurrence(conn, _config_at(path, tmp_path, "public"), reach="public")
        child = db.get_task(conn, db.create_task(
            conn, prompt="c", user_id=USER, source_type="subtask", parent_task_id=task.id,
        ))
        assert reach_for_task(conn, child, skill_index=INDEX).public

    def test_public_under_limited_stays_limited(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "limited")
        reach = reach_for_task(conn, _occurrence(conn, cfg, reach="public"), skill_index=INDEX)
        assert not reach.public and reach.egress_hosts == frozenset()

    @pytest.mark.parametrize("fields", [{}, {"reach": "limited"}, {"reach": "full"}])
    def test_public_widens_nothing_else(self, conn, path, tmp_path, fields):
        cfg = _config_at(path, tmp_path, "public")
        reach = reach_for_task(conn, _occurrence(conn, cfg, **fields), skill_index=INDEX)
        assert not reach.public and reach.egress_hosts == frozenset()

    @pytest.mark.parametrize("fields", [{}, {"reach": "full"}])
    def test_full_runs_unlimited_with_no_grant(self, conn, path, tmp_path, fields):
        cfg = _config_at(path, tmp_path, "full")
        reach = reach_for_task(conn, _occurrence(conn, cfg, **fields), skill_index=INDEX)
        assert reach.egress_hosts is None
        assert reach.egress_withheld == frozenset()
        assert not reach.public
        # Deliberately what an approved `open:` gets, and what the docs tell the operator.
        assert not reach.isolated_browser

    def test_a_subtask_of_a_full_occurrence_is_unlimited(self, conn, path, tmp_path):
        task = _occurrence(conn, _config_at(path, tmp_path, "full"))
        child = db.get_task(conn, db.create_task(
            conn, prompt="c", user_id=USER, source_type="subtask", parent_task_id=task.id,
        ))
        assert reach_for_task(conn, child, skill_index=INDEX).egress_hosts is None

    def test_a_job_saying_public_is_never_worse_off_under_full(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "full")
        assert reach_for_task(conn, _occurrence(conn, cfg, reach="public"), skill_index=INDEX).public

    def test_a_job_saying_limited_stays_limited_under_full(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "full")
        reach = reach_for_task(conn, _occurrence(conn, cfg, reach="limited"), skill_index=INDEX)
        assert reach.egress_hosts == frozenset()

    def test_it_covers_jobs_and_nothing_else(self, conn):
        task = db.get_task(conn, db.create_task(
            conn, prompt="p", user_id=USER, source_type="istota_file",
        ))
        assert reach_for_task(conn, task, skill_index=INDEX).egress_hosts == frozenset()

    def test_the_answer_is_the_occurrences_not_the_configs(self, conn, path, tmp_path):
        """A config change moves nothing already queued."""
        task = _occurrence(conn, _config_at(path, tmp_path, "limited"), reach="public")
        assert not reach_for_task(conn, task, skill_index=INDEX).public

    def test_the_posture_survives_the_once_row_going(self, conn, path, tmp_path):
        task = _occurrence(conn, _config_at(path, tmp_path, "public"), reach="public", once=True)
        conn.execute("DELETE FROM scheduled_jobs")
        conn.commit()
        assert reach_for_task(conn, task, skill_index=INDEX).public


class TestAnApprovalBeatsTheSetting:
    def test_an_approved_full_job_asking_for_public_runs_as_the_page_says(
        self, conn, path, tmp_path,
    ):
        cfg = _config_at(path, tmp_path, "public")
        _sync(conn, _job(reach="public"), ceiling="public")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "open", egress.job_digest(row), decided_via="web")
        conn.commit()
        task = _occurrence(conn, cfg, reach="public")
        view = egress.describe_subject(conn, USER, SUBJECT, ceiling="public")
        reach = reach_for_task(conn, task, skill_index=INDEX)
        assert view["state"] == "open" and view["by_default"] is None
        assert not reach.public and reach.egress_hosts is None

    def test_a_stale_approval_does_not_count(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "public")
        _sync(conn, _job(reach="public"), ceiling="public")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "open", egress.job_digest(row), decided_via="web")
        conn.commit()
        task = _occurrence(conn, cfg, reach="public", prompt="different text")
        assert reach_for_task(conn, task, skill_index=INDEX).public


class TestPublishingUnreviewed:
    def _trusted(self, conn, task, flag=True):
        job = db.get_user_scheduled_jobs(conn, USER)[0]
        job.publish_shared_kv_trusted = flag
        return scheduler._publish_is_trusted(conn, task, job)

    def test_an_occurrence_the_setting_let_run_is_not_trusted(self, conn, path, tmp_path):
        task = _occurrence(conn, _config_at(path, tmp_path, "public"), reach="public")
        assert not self._trusted(conn, task)

    def test_an_approved_one_keeps_its_flag(self, conn, path, tmp_path):
        task = _occurrence(conn, _config_at(path, tmp_path, "limited"), reach="public")
        assert self._trusted(conn, task)
        assert not self._trusted(conn, task, flag=False)


class TestAskingUnderEachSetting:
    def test_editing_an_approved_job_the_setting_covers_asks_nothing(self, conn):
        _sync(conn, _job(reach="public"), ceiling="limited")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "public", egress.job_digest(row), decided_via="web")
        conn.commit()
        assert _sync(conn, _job(reach="public", prompt="edited"), ceiling="public") == []
        assert _asked(conn) == set()

    def test_editing_an_approved_job_with_no_reach_under_full_asks_nothing(self, conn):
        _sync(conn, _job(), ceiling="full")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "open", egress.job_digest(row), decided_via="web")
        conn.commit()
        assert _sync(conn, _job(prompt="edited"), ceiling="full") == []
        assert _asked(conn) == set()

    def test_editing_an_approved_full_job_past_the_setting_asks_again(self, conn):
        _sync(conn, _job(reach="full"), ceiling="public")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "open", egress.job_digest(row), decided_via="web")
        conn.commit()
        assert _sync(conn, _job(reach="full", prompt="edited"), ceiling="public") == [SUBJECT]
        assert _asked(conn) == {"open"}

    def test_removing_an_approval_does_not_ask_again(self, conn):
        _sync(conn, _job(reach="full", egress=("example.com",)), ceiling="public")
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        digest = egress.job_digest(row)
        egress.approve_job(conn, USER, SUBJECT, "open", digest, decided_via="web")
        egress.remove_grant(conn, USER, SUBJECT, f"open:{digest}")
        conn.commit()
        job = _job(reach="full", egress=("example.com",))
        assert _sync(conn, job, ceiling="public") == []
        rows = {row["entry"]: row["dismissed"] for row in egress.list_refused(conn, USER, SUBJECT)}
        assert rows == {"open": 1, "example.com": 0}

    @pytest.mark.parametrize("reach,ceiling,asked", [
        ("public", "limited", {"public"}), ("public", "public", set()), ("public", "full", set()),
        ("full", "limited", {"open"}), ("full", "public", {"open"}), ("full", "full", set()),
        ("", "limited", set()), ("limited", "full", set()),
    ])
    def test_a_job_asks_for_what_it_will_not_get(self, conn, reach, ceiling, asked):
        _sync(conn, _job(reach=reach), ceiling=ceiling)
        assert _asked(conn) == asked

    def test_a_request_goes_once_the_setting_covers_it(self, conn):
        _sync(conn, _job(reach="public"), ceiling="limited")
        _sync(conn, _job(reach="public"), ceiling="public")
        assert _asked(conn) == set()

    def test_a_request_returns_if_the_setting_narrows(self, conn):
        _sync(conn, _job(reach="public"), ceiling="public")
        _sync(conn, _job(reach="public"), ceiling="limited")
        assert _asked(conn) == {"public"}

    def test_a_dismissed_request_does_not_ask_again(self, conn):
        _sync(conn, _job(reach="full"), ceiling="public")
        egress.dismiss_refused(conn, USER, SUBJECT, "open")
        conn.commit()
        assert _sync(conn, _job(reach="full"), ceiling="public") == []

    def test_changing_reach_withdraws_the_request(self, conn):
        _sync(conn, _job(reach="full"), ceiling="public")
        _sync(conn, _job(reach="limited"), ceiling="public")
        assert _asked(conn) == set()

    def test_declared_entries_ask_only_where_the_job_runs_limited(self, conn):
        _sync(conn, _job(egress=("example.com",), reach="public"), ceiling="public")
        assert _asked(conn) == set()
        _sync(conn, _job(egress=("example.com",)), ceiling="public")
        assert _asked(conn) == {"example.com"}


class TestOnceJobsUnderEachSetting:
    @pytest.mark.parametrize("fields,ceiling,fires", [
        ({"reach": "public"}, "limited", False), ({"reach": "public"}, "public", True),
        ({"reach": "public"}, "full", True),
        ({"reach": "full"}, "limited", False), ({"reach": "full"}, "public", False),
        ({"reach": "full"}, "full", True),
        ({"egress": ("example.com",)}, "public", False),
        ({"egress": ("example.com",)}, "full", True),
        # It runs public, which has open web, so the host is not what it waits on.
        ({"egress": ("example.com",), "reach": "public"}, "public", True),
    ])
    def test_it_fires_or_waits(self, conn, path, tmp_path, fields, ceiling, fires):
        cfg = _config_at(path, tmp_path, ceiling)
        _due_once_job(conn, **fields)
        assert bool(_tick(conn, cfg)) is fires

    def test_a_full_once_job_fires_once_approved(self, conn, path, tmp_path):
        cfg = _config_at(path, tmp_path, "public")
        _due_once_job(conn, reach="full")
        assert _tick(conn, cfg) == []
        row = egress.job_row_by_subject(conn, USER, SUBJECT)
        egress.approve_job(conn, USER, SUBJECT, "open", egress.job_digest(row), decided_via="web")
        conn.commit()
        assert len(_tick(conn, cfg)) == 1


class TestTheOneView:
    @pytest.mark.parametrize("reach,ceiling,by_default", [
        ("", "public", None), ("public", "public", "public"), ("public", "limited", None),
        ("", "full", "open"), ("full", "full", "open"), ("full", "public", None),
    ])
    def test_it_says_what_a_job_runs_as(self, conn, reach, ceiling, by_default):
        _sync(conn, _job(reach=reach), ceiling=ceiling)
        view = egress.describe_subject(conn, USER, SUBJECT, ceiling=ceiling)
        assert view["by_default"] == by_default

    def test_a_full_job_waits_on_full_access(self, conn):
        _sync(conn, _job(reach="full", once=True), ceiling="public")
        view = egress.describe_subject(conn, USER, SUBJECT, ceiling="public")
        assert view["job"]["waiting"] and view["waiting_on"] == []


class TestDoctor:
    def test_full_warns(self, path, tmp_path):
        from istota.doctor import WARN, check_egress_open_jobs

        result = check_egress_open_jobs(_config_at(path, tmp_path, "full"), False)
        assert result.status == WARN
        assert "scheduled_job_max_unapproved" in result.detail

    def test_public_does_not(self, path, tmp_path):
        from istota.doctor import OK, check_egress_open_jobs

        assert check_egress_open_jobs(_config_at(path, tmp_path, "public"), False).status == OK


