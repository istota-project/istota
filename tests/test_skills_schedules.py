"""`istota-skill schedules check`: the scheduler's own reading of CRON.md, from a task (#959).

The model wrote `egress = ["plaguewatch.test", "skill:feeds"]`, read the file
back, and reported it "still reads cleanly". It had no way to ask the loader:
`feeds` is a `local` skill, which no `skill:` entry may name, and the sync
dropped the entry while Settings, Scheduled jobs said the file had a problem.
So `check` is `cron_document_errors` on the file as it is, and nothing else.
"""

import json
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config
from istota.cron_loader import cron_document_errors
from istota.skills._loader import load_skill_index
from istota.skills._types import SkillMeta
from istota.skills.schedules import main
from istota.storage import get_user_cron_path

USER = "alice"
EXTERNAL = {"browse": SkillMeta(name="browse", description="x", egress="external")}


@pytest.fixture
def mount(tmp_path):
    path = tmp_path / "mount"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path, mount):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(db_path=db_path, workspace_path=mount, temp_dir=tmp_path / "temp")


@pytest.fixture(autouse=True)
def _env(monkeypatch, config):
    monkeypatch.setenv("ISTOTA_USER_ID", USER)
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)


def _write(mount, content):
    path = mount / get_user_cron_path(USER, "istota").lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _document(body: str) -> str:
    return (
        "# Scheduled Jobs\n\n```toml\n[[jobs]]\nname = \"probe\"\n"
        f"cron = \"0 9 * * *\"\nprompt = \"p\"\n{body}\n```\n"
    )


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr().out
    return json.loads(out), code


class TestTheReportedCase:
    def test_a_local_skill_in_egress_is_a_problem(self, capsys, mount):
        _write(mount, _document('egress = ["plaguewatch.test", "skill:feeds"]'))
        with patch("istota.skills._loader.usable_external_skills", return_value=EXTERNAL):
            out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["status"] == "error"
        assert out["reason"] == "problems"
        assert len(out["problems"]) == 1
        assert "skill:feeds" in out["problems"][0]

    def test_the_same_list_without_it_is_clean(self, capsys, mount):
        _write(mount, _document('egress = ["plaguewatch.test", "skill:browse"]'))
        with patch("istota.skills._loader.usable_external_skills", return_value=EXTERNAL):
            out, code = _run(capsys, ["check"])
        assert code == 0
        assert out["status"] == "ok"
        assert out["problems"] == []
        assert out["jobs"] == ["probe"]

    def test_it_names_what_a_skill_entry_may_take(self, capsys, mount):
        _write(mount, _document(""))
        with patch("istota.skills._loader.usable_external_skills", return_value=EXTERNAL):
            out, _ = _run(capsys, ["check"])
        assert out["egress_skills"] == ["browse"]


class TestItIsTheLoadersReading:
    @pytest.mark.parametrize("body", [
        'egress = ["skill:feeds"]',
        'egress = "example.com"',
        'egress = ["open:' + "a" * 64 + '"]',
        'reach = "everything"',
        'effort = 5',
        "",
    ])
    def test_it_reports_exactly_what_the_web_editor_does(self, capsys, mount, config, body):
        content = _document(body)
        _write(mount, content)
        with patch("istota.skills._loader.usable_external_skills", return_value=EXTERNAL):
            expected = cron_document_errors(content, config, USER)
            out, _ = _run(capsys, ["check"])
        assert out["problems"] == expected

    def test_a_toml_error_holds_the_whole_file(self, capsys, mount):
        _write(mount, "```toml\n[[jobs]]\nname = \"a\n```\n")
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["problems"] and out["problems"][0].startswith("TOML error")

    @pytest.mark.parametrize("content", [
        '[[jobs]]\nname = "a"\ncron = "0 9 * * *"\nprompt = "p"\n',
        '```toml\n# [[jobs]]\n# name = "a"\n```\n',
    ])
    def test_jobs_outside_a_fence_are_a_problem(self, capsys, mount, content):
        """The sync reads this as a file with no jobs, and rewrites it from the
        table when the user has any, which loses the edit."""
        _write(mount, content)
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["jobs"] == []
        assert "toml" in out["problems"][0]

    def test_an_empty_fence_is_the_users_empty_list(self, capsys, mount):
        _write(mount, "# Scheduled Jobs\n\n```toml\n```\n")
        out, code = _run(capsys, ["check"])
        assert code == 0
        assert out["jobs"] == []

    def test_no_file_is_clean_and_says_so(self, capsys):
        out, code = _run(capsys, ["check"])
        assert code == 0
        assert out["exists"] is False
        assert out["problems"] == []


class TestReach:
    @pytest.mark.parametrize("scope", ["files", "schedules"])
    def test_a_turn_without_the_users_files_is_refused(self, capsys, mount, monkeypatch, scope):
        _write(mount, _document('egress = ["skill:feeds"]'))
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", f"memory,{scope}")
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["reason"] == "scope_withheld"
        assert "problems" not in out

    def test_an_unreadable_file_is_not_reported_clean(self, capsys, mount):
        path = mount / get_user_cron_path(USER, "istota").lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        (mount / "elsewhere.md").write_text(_document(""))
        path.symlink_to(mount / "elsewhere.md")
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["reason"] == "unreadable"


def test_the_skill_ships_a_cli_and_tells_the_model_to_run_it():
    from pathlib import Path

    import istota.skills as skills_pkg

    bundled = Path(skills_pkg.__file__).parent
    index = load_skill_index(bundled / "nonexistent", bundled_dir=bundled)
    assert index["schedules"].cli is True
    for name in ("schedules", "reminders"):
        assert "istota-skill schedules check" in (bundled / name / "skill.md").read_text()


class TestAScheduleCroniterRefuses:
    """Found beside #959: `cron` was checked for presence only, so `check` and the
    web editor passed `cron = "not a cron"`, and the stored row then raised out of
    `check_scheduled_jobs` before any later job, for every user, every tick."""

    def test_check_reports_it(self, capsys, mount):
        _write(mount, _document("").replace('"0 9 * * *"', '"0 9 * *"'))
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["jobs"] == []
        assert "is not a cron schedule" in out["problems"][0]

    def test_a_stored_row_does_not_stop_other_users_jobs(self, config):
        from datetime import UTC, datetime, timedelta

        from istota import scheduler
        from istota.cron_loader import CronJob, sync_cron_jobs_to_db

        long_ago = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, "alice", [CronJob(name="bad", cron="0 9 * * *", prompt="x")])
            sync_cron_jobs_to_db(conn, "bob", [CronJob(name="good", cron="* * * * *", prompt="y")])
            conn.execute(
                "UPDATE scheduled_jobs SET cron_expression = 'not a cron' WHERE user_id = 'alice'"
            )
            conn.execute("UPDATE scheduled_jobs SET created_at = ?", (long_ago,))
            conn.commit()
            with patch.object(scheduler, "_sync_cron_files"):
                created = scheduler.check_scheduled_jobs(conn, config)
            assert [db.get_task(conn, t).user_id for t in created] == ["bob"]


class TestAZoneTheServerCannotLoad:
    """#976: a job's `timezone` that does not load drops the job, as a bad cron does,
    and `check` says which job and why."""

    def test_check_reports_it(self, capsys, mount):
        _write(mount, _document('timezone = "America/NewYork"'))
        out, code = _run(capsys, ["check"])
        assert code == 1
        assert out["jobs"] == []
        assert out["problems"] == [
            "Job 'probe': timezone 'America/NewYork' is not a timezone Istota knows"]

    def test_a_real_zone_is_clean(self, capsys, mount):
        _write(mount, _document('timezone = "America/New_York"'))
        out, code = _run(capsys, ["check"])
        assert code == 0
        assert out["jobs"] == ["probe"]
