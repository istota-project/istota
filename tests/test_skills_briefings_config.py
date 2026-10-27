"""`istota-skill briefings_config shared-block show`: an admin reads a shared block."""

import json

import pytest

from istota import db
from istota.briefings import shared_block_runs
from istota.config import Config
from istota.skills import briefings_config as skill
from istota.skills.briefings_config import main

PROMPT = "Read the front pages.\nList the stories with links."


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch, db_path):
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))


def _writers(monkeypatch, db_path, admins):
    monkeypatch.setattr(
        skill, "_load_config",
        lambda: Config(db_path=db_path, admin_users=set(admins)),
    )


def _agent_block(db_path, name="world-agent"):
    with db.get_db(db_path) as conn:
        db.upsert_shared_block_config(
            conn, name=name, cron="50 5 * * *", title="World",
            directive=PROMPT, render_mode="agent",
        )


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as exc:
        code = exc.code
    return json.loads(capsys.readouterr().out), code


class TestShow:
    def test_prints_the_whole_directive_and_definition(self, db_path, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 0
        block = out["shared_block"]
        assert block["directive"] == PROMPT
        assert block["render_mode"] == "agent"
        assert block["cron"] == "50 5 * * *"
        assert block["timezone"] == "UTC"
        assert out["run"]["agent"] is None

    def test_includes_the_agent_run_state(self, db_path, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        entry = {"task_id": 42, "briefing_name": "shared_block:world-agent:x",
                 "queued_at": "2026-10-27T05:50:00Z", "captured": True,
                 "outcome": "regenerated", "answered_at": "2026-10-27T06:10:00Z"}
        with db.get_db(db_path) as conn:
            db.shared_kv_set(conn, shared_block_runs.RUN_NAMESPACE,
                             shared_block_runs.AGENT_PREFIX + "world-agent",
                             json.dumps(entry), "daemon")
            db.set_briefing_shared_block_last_run(conn, "world-agent", "2026-10-27T05:50:00")

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 0
        assert out["run"]["agent"] == entry
        assert out["run"]["last_cron_run_at"] == "2026-10-27T05:50:00"

    def test_an_unknown_name_is_not_found(self, db_path, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})

        out, code = _run(capsys, ["shared-block", "show", "nope"])

        assert code == 1
        assert out["reason"] == "not_found"

    def test_a_non_writer_is_refused(self, db_path, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"bob"})
        _agent_block(db_path)

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 1
        assert out["reason"] == "not_a_shared_content_writer"
        assert PROMPT not in json.dumps(out)


class TestTheRealConfigLoad:
    """Through `load_config` and the admins file, not a patched config."""

    def _config_file(self, tmp_path, db_path):
        path = tmp_path / "config.toml"
        path.write_text(f'db_path = "{db_path}"\n')
        return path

    def test_a_named_admin_reads_the_block(self, tmp_path, db_path, monkeypatch, capsys):
        admins = tmp_path / "admins"
        admins.write_text("alice\n")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(self._config_file(tmp_path, db_path)))
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
        _agent_block(db_path)

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 0
        assert out["shared_block"]["directive"] == PROMPT

    def test_an_empty_admins_file_lets_nobody_in(self, tmp_path, db_path, monkeypatch, capsys):
        admins = tmp_path / "admins"
        admins.write_text("")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(self._config_file(tmp_path, db_path)))
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
        _agent_block(db_path)

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 1
        assert out["reason"] == "not_a_shared_content_writer"
