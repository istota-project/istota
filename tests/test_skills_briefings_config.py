"""`istota-skill briefings_config shared-block show|set-prompt`: an admin reads
and re-prompts a shared block."""

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


@pytest.fixture
def deferred(tmp_path):
    path = tmp_path / "deferred"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch, db_path, deferred):
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.delenv("ISTOTA_WORKSPACE_PATH", raising=False)
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)


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


NEW_PROMPT = "Read the front pages in three languages.\nLink every story.\n"


def _row(db_path, name="world-agent"):
    with db.get_db(db_path) as conn:
        return db.get_shared_block_config(conn, name)


def _set_prompt(capsys, path, name="world-agent"):
    return _run(capsys, ["shared-block", "set-prompt", name, "--prompt-file", str(path)])


class TestSetPrompt:
    def test_a_writer_replaces_only_the_directive(self, db_path, deferred, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE shared_block_configs SET enabled = 0 WHERE name = 'world-agent'",
            )
        before = _row(db_path)
        prompt = deferred / "prompt.md"
        prompt.write_text(NEW_PROMPT)

        out, code = _set_prompt(capsys, prompt)

        assert code == 0, out
        after = _row(db_path)
        assert after.directive == NEW_PROMPT
        assert (after.name, after.cron, after.title, after.render_mode,
                after.enabled, after.trusted, after.sources) == (
            before.name, before.cron, before.title, before.render_mode,
            before.enabled, before.trusted, before.sources)
        assert after.enabled is False

    def test_a_non_writer_is_refused(self, db_path, deferred, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"bob"})
        _agent_block(db_path)
        prompt = deferred / "prompt.md"
        prompt.write_text(NEW_PROMPT)

        out, code = _set_prompt(capsys, prompt)

        assert code == 1
        assert out["reason"] == "not_a_shared_content_writer"
        assert _row(db_path).directive == PROMPT

    @pytest.mark.parametrize("content", ["", "  \n\t\n"])
    def test_an_empty_file_is_refused(self, db_path, deferred, monkeypatch, capsys, content):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        prompt = deferred / "prompt.md"
        prompt.write_text(content)

        out, code = _set_prompt(capsys, prompt)

        assert code == 1
        assert out["reason"] == "empty_prompt"
        assert _row(db_path).directive == PROMPT

    def test_a_path_outside_the_task_is_refused(self, tmp_path, db_path, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        outside = tmp_path / "elsewhere" / "prompt.md"
        outside.parent.mkdir()
        outside.write_text(NEW_PROMPT)

        out, code = _set_prompt(capsys, outside)

        assert code == 1
        assert out["reason"] == "host_path_refused"
        assert _row(db_path).directive == PROMPT

    def test_a_symlink_at_the_leaf_is_not_followed(
        self, tmp_path, db_path, deferred, monkeypatch, capsys,
    ):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("not a prompt")
        (deferred / "prompt.md").symlink_to(secret)

        out, code = _set_prompt(capsys, deferred / "prompt.md")

        assert code == 1
        assert _row(db_path).directive == PROMPT

    def test_a_definition_the_validator_refuses_is_left_unchanged(
        self, db_path, deferred, monkeypatch, capsys,
    ):
        # A stored agent row with sources (written past the validator) cannot
        # be saved again, so the new prompt must not land either.
        _writers(monkeypatch, db_path, {"alice"})
        with db.get_db(db_path) as conn:
            db.upsert_shared_block_config(
                conn, name="world-agent", cron="50 5 * * *", directive=PROMPT,
                render_mode="agent", sources=[{"kind": "markets", "config": {}}],
            )
        before = _row(db_path)
        prompt = deferred / "prompt.md"
        prompt.write_text(NEW_PROMPT)

        out, code = _set_prompt(capsys, prompt)

        assert code == 1
        assert out["reason"] == "invalid_definition"
        assert "no sources" in out["error"]
        assert _row(db_path) == before

    def test_an_unknown_name_is_not_found(self, db_path, deferred, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        prompt = deferred / "prompt.md"
        prompt.write_text(NEW_PROMPT)

        out, code = _set_prompt(capsys, prompt, name="nope")

        assert code == 1
        assert out["reason"] == "not_found"
        assert _row(db_path, "nope") is None

    def test_show_reads_back_what_set_prompt_wrote(self, db_path, deferred, monkeypatch, capsys):
        _writers(monkeypatch, db_path, {"alice"})
        _agent_block(db_path)
        prompt = deferred / "prompt.md"
        prompt.write_text(NEW_PROMPT)
        _set_prompt(capsys, prompt)

        out, code = _run(capsys, ["shared-block", "show", "world-agent"])

        assert code == 0
        assert out["shared_block"]["directive"] == NEW_PROMPT


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
