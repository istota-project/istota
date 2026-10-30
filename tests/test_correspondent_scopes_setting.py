"""The per-user widening of what a correspondent's turn keeps: the column, the
`[users.X]` key, `istota user ensure`, the settings plan, and the one reader.

Through the real stores and the real CLI. The reach it produces is driven through
`execute_task` in `test_email_thread_reach.py`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from istota import cli, db, user_profiles
from istota.config import UserConfig, load_config
from istota.lib import toml_write
from istota.rooms import scopes
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan

SCOPES_LOGGER = "istota.rooms.scopes"
REFUSED = ["files", "memory", "recall", "memory_search"]
INDEX = {
    name: SimpleNamespace(shared_room="private")
    for name in ("email", "skills", "transcribe", "whisper", "markets", "calendar", "kv")
}


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    (tmp_path / "admins").write_text("alice\n")
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({
        "db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
        "temp_dir": str(tmp_path / "temp"), "default_briefings": [],
        "briefings": {"shared_blocks": []},
    }))
    return config, database


@pytest.fixture(autouse=True)
def _warnings_start_fresh(monkeypatch):
    monkeypatch.setattr(scopes, "_warned_correspondent_names", set())


def _stored(database, user="alice"):
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT correspondent_scopes FROM user_profiles WHERE user_id = ?", (user,),
        ).fetchone()
    return row[0] if row else None


def _hand_write(database, raw, user="alice"):
    user_profiles.ensure_profile(database, user)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE user_profiles SET correspondent_scopes = ? WHERE user_id = ?", (raw, user),
        )


def _allowance(database, user="alice", index=INDEX):
    with db.get_db(database) as conn:
        return scopes.correspondent_scopes(conn, user, skill_index=index)


def _invoke(monkeypatch, capsys, config, *arguments):
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), *map(str, arguments)])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    output = capsys.readouterr()
    return code, output.out, output.err


def _preview(install, profile):
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {"profile": profile}}})
    return context, engine.build_plan(plan, context, engine.Options())


class TestTheMigration:
    def test_it_is_idempotent_and_leaves_old_rows_null(self, install):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        with sqlite3.connect(database) as conn:
            conn.execute("ALTER TABLE user_profiles DROP COLUMN correspondent_scopes")
        db.init_db(database)
        db.init_db(database)
        with sqlite3.connect(database) as conn:
            db._migrate_correspondent_scopes(conn)
        assert _stored(database) is None
        assert user_profiles.get_profile(database, "alice").correspondent_scopes == []
        assert _allowance(database).scopes == {"email", "skills", "transcribe", "whisper", "markets"}

    def test_a_row_written_before_the_migration_ran_passes_the_strict_read(self, install):
        _, database = install
        _hand_write(database, None)
        with sqlite3.connect(database) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM user_profiles").fetchone()
        assert user_profiles._row_to_profile(row, strict=True).correspondent_scopes == []


class TestTheConfigKey:
    def _write(self, tmp_path, value):
        path = tmp_path / "keyed.toml"
        path.write_text(toml_write.dumps({
            "db_path": str(tmp_path / "keyed.db"),
            "users": {"alice": {"correspondent_scopes": value}},
        }))
        return path

    def test_a_list_is_accepted(self, tmp_path):
        config = load_config(self._write(tmp_path, ["calendar", "web"]))
        assert config.users["alice"].correspondent_scopes == ["calendar", "web"]

    def test_an_unknown_name_survives_the_load(self, tmp_path):
        config = load_config(self._write(tmp_path, ["removed-skill"]))
        assert config.users["alice"].correspondent_scopes == ["removed-skill"]

    @pytest.mark.parametrize("name", REFUSED)
    def test_a_refused_name_is_a_config_error_naming_the_key(self, tmp_path, name):
        with pytest.raises(ValueError, match=rf"users\.alice.*correspondent_scopes.*{name}"):
            load_config(self._write(tmp_path, ["calendar", name]))

    @pytest.mark.parametrize("value", ["calendar", ["calendar", 3]])
    def test_a_value_that_is_not_a_list_of_strings_is_refused(self, tmp_path, value):
        with pytest.raises(ValueError, match="correspondent_scopes"):
            load_config(self._write(tmp_path, value))


class TestTheDatabaseOverTheConfig:
    def test_toml_seeds_a_new_row(self, install):
        _, database = install
        users = {"alice": UserConfig(correspondent_scopes=["calendar"])}
        assert user_profiles.import_from_user_configs(database, users) == 1
        assert json.loads(_stored(database)) == ["calendar"]
        assert _allowance(database).scopes >= {"calendar"}

    def test_a_first_login_seeds_from_toml_too(self, install):
        _, database = install
        seed = UserConfig(correspondent_scopes=["calendar"])
        user_profiles.ensure_profile(database, "alice", seed_from=seed)
        assert json.loads(_stored(database)) == ["calendar"]

    def test_an_existing_row_wins(self, install):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        user_profiles.update_profile(database, "alice", correspondent_scopes=["kv"])
        users = {"alice": UserConfig(correspondent_scopes=["calendar"])}
        user_profiles.import_from_user_configs(database, users)
        assert json.loads(_stored(database)) == ["kv"]
        allowance = _allowance(database)
        assert "kv" in allowance.scopes and "calendar" not in allowance.scopes

    def test_the_overlay_replaces_the_toml_and_says_so_once(self, install, caplog):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        profile = user_profiles.get_profile(database, "alice")
        user_config = UserConfig(correspondent_scopes=["calendar"])
        with caplog.at_level(logging.WARNING, logger="istota.user_profiles"):
            user_profiles.merge_into_user_config(profile, user_config)
            user_profiles.merge_into_user_config(profile, user_config)
        assert user_config.correspondent_scopes == []
        lines = [r for r in caplog.records if "correspondent_scopes" in r.getMessage()]
        assert len(lines) == 1
        assert "istota user ensure" in lines[0].getMessage()


class TestTheProfileStore:
    @pytest.mark.parametrize("name", REFUSED)
    def test_no_writer_can_store_a_refused_name(self, install, name):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        with pytest.raises(ValueError, match=name):
            user_profiles.update_profile(database, "alice", correspondent_scopes=[name])
        with pytest.raises(ValueError, match=name):
            user_profiles.upsert_profile(
                database,
                user_profiles.UserProfile(user_id="alice", correspondent_scopes=[name]),
            )
        assert not _stored(database) or json.loads(_stored(database)) == []

    def test_only_an_admin_writes_it_on_the_web(self):
        assert "correspondent_scopes" not in user_profiles.SELF_EDITABLE_FIELDS
        assert "correspondent_scopes" in user_profiles.ADMIN_EDITABLE_FIELDS


class TestUserEnsure:
    def test_the_flag_replaces_the_list_and_show_reports_it(self, install, monkeypatch, capsys):
        config, database = install
        code, _, err = _invoke(
            monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
            "--correspondent-scope", "calendar", "--correspondent-scope", "web",
        )
        assert code == 0, err
        assert json.loads(_stored(database)) == ["calendar", "web"]

        code, _, err = _invoke(
            monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
            "--correspondent-scope", "kv",
        )
        assert code == 0, err
        assert json.loads(_stored(database)) == ["kv"]

        code, out, err = _invoke(monkeypatch, capsys, config, "user", "show", "--name", "alice")
        assert code == 0, err
        assert json.loads(out)["correspondent_scopes"] == ["kv"]

    def test_an_ensure_without_the_flag_leaves_it_alone(self, install, monkeypatch, capsys):
        config, database = install
        _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                "--correspondent-scope", "calendar")
        _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice", "--tz", "UTC")
        assert json.loads(_stored(database)) == ["calendar"]

    def test_clear_empties_it(self, install, monkeypatch, capsys):
        config, database = install
        _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                "--correspondent-scope", "calendar")
        code, _, err = _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                               "--clear-correspondent-scopes")
        assert code == 0, err
        assert json.loads(_stored(database)) == []
        assert _allowance(database).web is False

    def test_the_two_flags_do_not_combine(self, install, monkeypatch, capsys):
        config, _ = install
        code, _, _ = _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                             "--correspondent-scope", "calendar", "--clear-correspondent-scopes")
        assert code != 0

    @pytest.mark.parametrize("name", REFUSED)
    def test_a_refused_name_writes_nothing(self, install, monkeypatch, capsys, name):
        config, database = install
        code, _, err = _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                               "--correspondent-scope", name)
        assert code == 1
        assert name in err
        assert user_profiles.get_profile(database, "alice") is None

    def test_seed_mode_keeps_a_stored_list(self, install, monkeypatch, capsys):
        config, database = install
        _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice",
                "--correspondent-scope", "calendar")
        _invoke(monkeypatch, capsys, config, "user", "ensure", "--name", "alice", "--seed",
                "--correspondent-scope", "kv")
        assert json.loads(_stored(database)) == ["calendar"]


class TestTheSettingsPlan:
    def test_apply_writes_and_a_second_apply_changes_nothing(self, install):
        _, database = install
        context, operations = _preview(install, {"correspondent_scopes": ["calendar", "web"]})
        assert engine.execute_plan(operations, context).exit_code == 2
        assert json.loads(_stored(database)) == ["calendar", "web"]
        before = database.read_bytes()
        context, operations = _preview(install, {"correspondent_scopes": ["calendar", "web"]})
        assert engine.execute_plan(operations, context).exit_code == 0
        assert database.read_bytes() == before

    @pytest.mark.parametrize("name", REFUSED)
    def test_a_refused_name_refuses_the_plan(self, install, name):
        _, database = install
        before = database.read_bytes()
        with pytest.raises(engine.ExecutionError, match="validation_error"):
            _preview(install, {"correspondent_scopes": [name]})
        assert database.read_bytes() == before

    def test_snapshot_emits_it_and_the_snapshot_applies_as_a_noop(self, install, monkeypatch, capsys):
        config, database = install
        context, operations = _preview(install, {"correspondent_scopes": ["calendar"]})
        engine.execute_plan(operations, context)
        code, out, err = _invoke(monkeypatch, capsys, config, "snapshot", "--format", "json",
                                 "--user", "alice")
        assert code == 0, err
        profile = json.loads(out)["users"]["alice"]["profile"]
        assert profile["correspondent_scopes"] == ["calendar"]
        before = database.read_bytes()
        context, operations = _preview(install, {"correspondent_scopes": profile["correspondent_scopes"]})
        assert engine.execute_plan(operations, context).exit_code == 0
        assert database.read_bytes() == before

    def test_a_snapshot_of_a_row_never_set_applies_as_a_noop(self, install, monkeypatch, capsys):
        config, database = install
        _hand_write(database, None)
        code, out, err = _invoke(monkeypatch, capsys, config, "snapshot", "--format", "json",
                                 "--user", "alice")
        assert code == 0, err
        assert json.loads(out)["users"]["alice"]["profile"]["correspondent_scopes"] == []
        context, operations = _preview(install, {"correspondent_scopes": []})
        assert engine.execute_plan(operations, context).exit_code == 0
        assert _stored(database) is None


class TestTheReader:
    def test_it_adds_the_widening_and_the_web_token(self, install):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        user_profiles.update_profile(database, "alice", correspondent_scopes=["calendar", "web"])
        allowance = _allowance(database)
        assert allowance.scopes == {"email", "skills", "transcribe", "whisper", "markets", "calendar"}
        assert allowance.web is True

    def test_web_is_not_a_scope_even_with_a_skill_absent(self, install):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        user_profiles.update_profile(database, "alice", correspondent_scopes=["web"])
        assert "web" not in _allowance(database).scopes
        assert _allowance(database, index={}).web is True

    def test_another_users_row_does_not_apply(self, install):
        _, database = install
        user_profiles.ensure_profile(database, "bob")
        user_profiles.update_profile(database, "bob", correspondent_scopes=["calendar", "web"])
        allowance = _allowance(database)
        assert "calendar" not in allowance.scopes and allowance.web is False

    def test_an_unknown_name_is_dropped_with_one_warning(self, install, caplog):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        user_profiles.update_profile(database, "alice", correspondent_scopes=["removed-skill"])
        with caplog.at_level(logging.WARNING, logger=SCOPES_LOGGER):
            first = _allowance(database)
            _allowance(database)
        assert "removed-skill" not in first.scopes
        assert len([r for r in caplog.records if "removed-skill" in r.getMessage()]) == 1

    def test_an_empty_index_warns_about_nothing_and_grants_nothing(self, install, caplog):
        _, database = install
        user_profiles.ensure_profile(database, "alice")
        user_profiles.update_profile(database, "alice", correspondent_scopes=["calendar"])
        with caplog.at_level(logging.WARNING, logger=SCOPES_LOGGER):
            allowance = _allowance(database, index={})
        assert allowance.scopes == frozenset()
        assert caplog.records == []

    @pytest.mark.parametrize("name", REFUSED)
    def test_a_refused_name_in_a_hand_edited_row_is_dropped(self, install, caplog, name):
        _, database = install
        _hand_write(database, json.dumps([name, "calendar"]))
        index = {**INDEX, "recall": SimpleNamespace(shared_room="private")}
        with caplog.at_level(logging.WARNING, logger=SCOPES_LOGGER):
            allowance = _allowance(database, index=index)
        assert name not in allowance.scopes and "calendar" in allowance.scopes
        assert [r for r in caplog.records if name in r.getMessage()]

    @pytest.mark.parametrize("name", ["files", "memory"])
    def test_a_refused_name_stays_withheld_on_an_empty_index(self, install, name):
        _, database = install
        _hand_write(database, json.dumps([name]))
        assert _allowance(database, index={}).scopes == frozenset()

    @pytest.mark.parametrize("raw", ['"calendar"', '{"calendar": 1}', "not json", '["calendar", 3]', "null"])
    def test_a_stored_value_that_is_not_a_list_of_strings_reads_empty(self, install, caplog, raw):
        _, database = install
        _hand_write(database, raw)
        with caplog.at_level(logging.WARNING, logger="istota.user_profiles"):
            allowance = _allowance(database)
        assert allowance.scopes == {"email", "skills", "transcribe", "whisper", "markets"}
        assert allowance.web is False
        assert [r for r in caplog.records if "correspondent_scopes" in r.getMessage()]

    def test_a_database_without_the_column_reads_the_default(self, tmp_path, caplog):
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.execute("CREATE TABLE user_profiles (user_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO user_profiles VALUES ('alice')")
        with caplog.at_level(logging.WARNING, logger="istota.user_profiles"):
            allowance = scopes.correspondent_scopes(conn, "alice", skill_index=INDEX)
        conn.close()
        assert allowance.scopes == {"email", "skills", "transcribe", "whisper", "markets"}
        assert caplog.records
