"""`[users.X.briefings]` in config.toml is a retired input, imported once.

The entries are parsed into `UserConfig.legacy_briefing_seeds`, reported as a
retired config problem, and copied into `briefing_configs` once per user under
a `_migration_state` sentinel. After that the table is the only authority, so a
briefing deleted in the settings page stays deleted across restarts.
"""

from __future__ import annotations

import sqlite3

from istota import config as cfgmod
from istota import db, user_briefings
from istota.config import BriefingConfig, Config, UserConfig, load_config
from istota.config_mapper import collect_problems


def _seed(name="morning", cron="0 7 * * *", **extra) -> BriefingConfig:
    return BriefingConfig(name=name, cron=cron, output="talk", conversation_token="room", **extra)


def _user(*seeds: BriefingConfig) -> UserConfig:
    return UserConfig(display_name="Alice", legacy_briefing_seeds=list(seeds))


def _sentinel_rows(db_path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT name FROM _migration_state WHERE name LIKE 'user_config_briefings_import_v1:%'"
        )]
    finally:
        conn.close()


def _write_config(tmp_path, db_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        "\n[users.alice]\n"
        'display_name = "Alice"\n'
        + body
    )
    return path


_MORNING = (
    "\n[[users.alice.briefings]]\n"
    'name = "morning"\n'
    'cron = "0 7 * * *"\n'
    'conversation_token = "room"\n'
    'output = "talk"\n'
)


class TestParsing:
    def test_entries_land_in_legacy_seeds_not_briefings(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        path = _write_config(tmp_path, tmp_path / "missing.db", _MORNING)
        config = load_config(path)
        alice = config.users["alice"]
        assert alice.briefings == []
        assert [seed.name for seed in alice.legacy_briefing_seeds] == ["morning"]
        assert alice.legacy_briefing_seeds[0].cron == "0 7 * * *"

    def test_the_block_is_reported_as_retired(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        path = _write_config(tmp_path, tmp_path / "missing.db", _MORNING)
        with collect_problems() as problems:
            load_config(path)
        retired = [p for p in problems if p["path"] == ("users", "alice", "briefings")]
        assert len(retired) == 1
        assert retired[0]["kind"] == "retired"
        assert "remove them from config.toml" in retired[0]["message"]

    def test_no_block_reports_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        path = _write_config(tmp_path, tmp_path / "missing.db", "")
        with collect_problems() as problems:
            load_config(path)
        assert not [p for p in problems if p["path"][:3] == ("users", "alice", "briefings")]


class TestOneTimeImport:
    def test_imports_and_sets_the_sentinel(self, db_path):
        written = user_briefings.import_from_user_configs(db_path, {"alice": _user(_seed())})
        assert written == 1
        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.enabled is True and row.cron == "0 7 * * *"
        assert _sentinel_rows(db_path) == ["user_config_briefings_import_v1:alice"]

    def test_a_deleted_briefing_stays_deleted(self, db_path):
        users = {"alice": _user(_seed())}
        user_briefings.import_from_user_configs(db_path, users)
        assert user_briefings.delete_briefing(db_path, "alice", "morning")
        assert user_briefings.import_from_user_configs(db_path, users) == 0
        assert user_briefings.get_briefing(db_path, "alice", "morning") is None

    def test_an_entry_added_after_the_sentinel_is_not_imported(self, db_path):
        user_briefings.import_from_user_configs(db_path, {"alice": _user(_seed())})
        later = {"alice": _user(_seed(), _seed(name="evening", cron="0 19 * * *"))}
        assert user_briefings.import_from_user_configs(db_path, later) == 0
        assert user_briefings.get_briefing(db_path, "alice", "evening") is None

    def test_a_user_with_no_entries_is_marked_done(self, db_path):
        user_briefings.import_from_user_configs(db_path, {"alice": _user()})
        assert _sentinel_rows(db_path) == ["user_config_briefings_import_v1:alice"]
        assert user_briefings.import_from_user_configs(db_path, {"alice": _user(_seed())}) == 0
        assert user_briefings.list_briefings(db_path, "alice") == []

    def test_a_cronless_entry_is_imported_disabled(self, db_path):
        written = user_briefings.import_from_user_configs(db_path, {"alice": _user(_seed(cron=""))})
        assert written == 1
        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.enabled is False and row.cron == ""

    def test_an_existing_row_wins_and_the_sentinel_is_still_set(self, db_path):
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 8 * * *",
            conversation_token="db-room", output="talk",
        )
        assert user_briefings.import_from_user_configs(db_path, {"alice": _user(_seed())}) == 0
        assert user_briefings.get_briefing(db_path, "alice", "morning").cron == "0 8 * * *"
        assert _sentinel_rows(db_path) == ["user_config_briefings_import_v1:alice"]

    def test_the_merged_briefings_list_is_not_an_input(self, db_path):
        # Only the legacy seeds are imported; a DB-derived entry is not re-written.
        user = UserConfig(briefings=[_seed(name="from-db")])
        assert user_briefings.import_from_user_configs(db_path, {"alice": user}) == 0
        assert user_briefings.list_briefings(db_path, "alice") == []

    def test_pending_until_the_import_runs(self, db_path):
        with db.get_db(db_path) as conn:
            assert user_briefings.config_import_pending(conn, "alice") is True
        user_briefings.import_from_user_configs(db_path, {"alice": _user()})
        with db.get_db(db_path) as conn:
            assert user_briefings.config_import_pending(conn, "alice") is False


class TestDefaultMigrationReport:
    def test_an_unimported_seed_needs_initialization_first(self, db_path):
        import pytest

        from istota.briefings import group_migrate
        from istota.groups.policy import GroupError

        config = Config(db_path=db_path, users={"alice": _user(_seed())})
        with db.get_db(db_path) as conn, pytest.raises(GroupError, match="legacy_personal_initialization_required"):
            group_migrate._current(conn, config, "alice", "morning")

    def test_a_deleted_import_is_not_current(self, db_path):
        from istota.briefings import group_migrate

        config = Config(db_path=db_path, users={"alice": _user(_seed())})
        user_briefings.import_from_user_configs(db_path, config.users)
        user_briefings.delete_briefing(db_path, "alice", "morning")
        with db.get_db(db_path) as conn:
            assert group_migrate._current(conn, config, "alice", "morning") is None


class TestDbOnlyOverlay:
    def test_an_unimported_seed_is_not_scheduled(self, db_path):
        config = Config(db_path=db_path, users={"alice": _user(_seed())})
        cfgmod._apply_user_briefings(config)
        assert config.users["alice"].briefings == []

    def test_rows_build_the_list(self, db_path):
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="evening", cron="0 19 * * *",
            conversation_token="room", output="talk",
        )
        config = Config(db_path=db_path, users={"alice": _user(_seed())})
        cfgmod._apply_user_briefings(config)
        assert [b.name for b in config.users["alice"].briefings] == ["evening"]

    def test_seed_blocks_ride_on_the_row_of_the_same_name(self, db_path):
        blocks = [{"title": "News", "sources": [{"kind": "rss", "config": {}}]}]
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 8 * * *",
            conversation_token="room", output="talk",
        )
        config = Config(db_path=db_path, users={"alice": _user(_seed(blocks=blocks))})
        cfgmod._apply_user_briefings(config)
        (morning,) = config.users["alice"].briefings
        assert morning.cron == "0 8 * * *"
        assert morning.blocks == blocks


class TestThroughLoadConfig:
    def test_import_once_then_delete_survives_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        database = tmp_path / "istota.db"
        db.init_db(database)
        path = _write_config(tmp_path, database, _MORNING)

        # A boot: load, import (as the scheduler does), overlay.
        config = load_config(path)
        user_briefings.import_from_user_configs(config.db_path, config.users)
        cfgmod._apply_user_briefings(config)
        assert [b.name for b in config.users["alice"].briefings] == ["morning"]

        # The owner deletes it in the settings page.
        assert user_briefings.delete_briefing(database, "alice", "morning")

        # The next boot does not bring it back.
        config = load_config(path)
        assert user_briefings.import_from_user_configs(config.db_path, config.users) == 0
        cfgmod._apply_user_briefings(config)
        assert config.users["alice"].briefings == []
