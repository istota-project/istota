"""`security.egress_open_jobs`: jobs still on full access keep H-2 open.

It counts and never names one: a job name is the user's and is model-writable.
"""

import pytest

from istota import db, doctor
from istota.config import Config
from istota.rooms import egress

NAME = "security.egress_open_jobs"


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(db_path=db_path)


def _job(config, name, *, grant=None, prompt="p", reach="private", user="alice"):
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, reach) "
            "VALUES (?, ?, '0 9 * * *', ?, ?)",
            (user, name, prompt, reach),
        )
        digest = egress.job_digest(
            {"prompt": prompt, "command": None, "skill": None, "skill_args": None},
        )
        for kind in grant or ():
            conn.execute(
                "INSERT INTO egress_grants (user_id, subject, entry, decided_via) "
                "VALUES (?, ?, ?, 'migration')",
                (user, egress.cron_subject(name), f"{kind}:{digest}"),
            )
        conn.commit()


def test_it_is_registered_as_a_deployment_check():
    assert NAME in dict(doctor.CHECKS)
    assert doctor.CHECK_SCOPES[NAME] == doctor.DEPLOYMENT


def test_no_database_is_a_skip(tmp_path):
    assert doctor.check_egress_open_jobs(Config(db_path=tmp_path / "absent.db"), False).status == doctor.SKIP


def test_no_open_job_is_ok(config):
    _job(config, "limited")
    assert doctor.check_egress_open_jobs(config, False).status == doctor.OK


def test_it_warns_with_a_count_and_names_no_job(config):
    _job(config, "canary-one", grant=["open"])
    _job(config, "canary-two", grant=["open"], user="bob")
    result = doctor.check_egress_open_jobs(config, False)
    assert result.status == doctor.WARN
    assert "2 " in result.detail
    assert "canary" not in result.detail
    assert "canary" not in (result.remedy or "")
    assert "Network access" in result.remedy


@pytest.mark.parametrize("name,kwargs", [
    ("_module.feeds.run_scheduled", {"grant": ["open"]}),
    ("both", {"grant": ["open", "public"], "reach": "public"}),
], ids=["module-row", "public-comes-first"])
def test_these_do_not_count(config, name, kwargs):
    _job(config, name, **kwargs)
    assert doctor.check_egress_open_jobs(config, False).status == doctor.OK


def test_a_stale_grant_or_a_removed_job_does_not_count(config):
    _job(config, "edited", grant=["open"])
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE scheduled_jobs SET prompt = 'changed'")
        conn.execute(
            "INSERT INTO egress_grants (user_id, subject, entry, decided_via) "
            "VALUES ('alice', 'cron:gone', ?, 'migration')",
            ("open:" + "0" * 64,),
        )
        conn.commit()
    assert doctor.check_egress_open_jobs(config, False).status == doctor.OK


def test_a_read_failure_is_a_warning(tmp_path):
    path = tmp_path / "bare.db"
    path.write_bytes(b"")
    result = doctor.check_egress_open_jobs(Config(db_path=path), False)
    assert result.status == doctor.WARN
    assert "could not read" in result.detail
