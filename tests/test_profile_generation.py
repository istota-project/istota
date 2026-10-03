"""The `profile_generation` counter and the live `user_profiles` refresh.

`config.users` is a snapshot each process takes at load. A profile written
after that (the settings page, `istota user ensure`) used to reach no running
process until it restarted. Triggers bump one counter on every write, and
`refresh_user_profiles_if_changed` re-applies the overlay when it moves.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import db, user_profiles
from istota.config import Config, UserConfig, refresh_user_profiles_if_changed

SCHEMA = Path(db.__file__).resolve().parent.parent.parent / "schema.sql"
TRIGGERS = ("user_profiles_gen_ai", "user_profiles_gen_au", "user_profiles_gen_ad")


def _generation(db_path) -> int:
    return user_profiles.read_profile_generation(db_path)


def _config(db_path, users=None) -> Config:
    return Config(db_path=db_path, users=dict(users or {}))


class TestTheTriggers:
    def test_insert_update_and_delete_each_bump(self, db_path):
        start = _generation(db_path)
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        after_insert = _generation(db_path)
        assert after_insert == start + 1

        user_profiles.update_profile(db_path, "alice", display_name="Alicia")
        after_update = _generation(db_path)
        assert after_update == after_insert + 1

        user_profiles.delete_profile(db_path, "alice")
        assert _generation(db_path) == after_update + 1

    def test_an_ensure_that_inserts_nothing_does_not_bump(self, db_path):
        """`ensure_profile` runs on every login; `ON CONFLICT DO NOTHING`
        fires no trigger, so a login does not make every process re-apply."""
        user_profiles.ensure_profile(db_path, "alice")
        before = _generation(db_path)
        user_profiles.ensure_profile(db_path, "alice", display_name="Other")
        assert _generation(db_path) == before


class TestTheRefresh:
    def test_unchanged_generation_is_a_noop(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        cfg = _config(db_path, {"alice": UserConfig(display_name="Alice")})
        assert refresh_user_profiles_if_changed(cfg) is True
        assert refresh_user_profiles_if_changed(cfg) is False

    def test_a_write_is_applied_on_the_next_call(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        cfg = _config(db_path, {"alice": UserConfig(display_name="Alice")})
        refresh_user_profiles_if_changed(cfg)
        assert cfg.find_user_by_email("alice@example.com") is None

        user_profiles.update_profile(
            db_path, "alice", email_addresses=["alice@example.com"],
        )
        assert refresh_user_profiles_if_changed(cfg) is True
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_a_known_user_is_updated_in_place(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        alice = UserConfig(display_name="Alice")
        cfg = _config(db_path, {"alice": alice})
        refresh_user_profiles_if_changed(cfg)
        user_profiles.update_profile(db_path, "alice", sms_phone_number="+15550001111")
        refresh_user_profiles_if_changed(cfg)
        assert cfg.users["alice"] is alice
        assert alice.sms_phone_number == "+15550001111"

    def test_a_new_row_replaces_the_dict_rather_than_inserting(self, db_path):
        """Inserting a key while another thread iterates `config.users` raises
        in that thread; the old dict must be left exactly as it was."""
        user_profiles.ensure_profile(db_path, "alice")
        cfg = _config(db_path, {"alice": UserConfig(display_name="Alice")})
        refresh_user_profiles_if_changed(cfg)
        old = cfg.users

        user_profiles.ensure_profile(db_path, "carol", display_name="Carol")
        assert refresh_user_profiles_if_changed(cfg) is True

        assert cfg.users is not old
        assert set(old) == {"alice"}
        assert cfg.users["carol"].display_name == "Carol"
        assert cfg.users["alice"] is old["alice"]

    def test_a_failed_overlay_keeps_the_snapshot_and_retries(
        self, db_path, monkeypatch, caplog,
    ):
        user_profiles.ensure_profile(db_path, "alice")
        cfg = _config(db_path, {"alice": UserConfig(display_name="Alice")})
        refresh_user_profiles_if_changed(cfg)
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["alice@example.com"],
        )

        def _boom(_path):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(user_profiles, "list_profiles", _boom)
        with caplog.at_level("WARNING", logger="istota.config"):
            assert refresh_user_profiles_if_changed(cfg) is False
        assert "user_profiles refresh failed" in caplog.text
        assert cfg.users["alice"].email_addresses == []

        monkeypatch.undo()
        assert refresh_user_profiles_if_changed(cfg) is True
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_min_interval_skips_the_read(self, db_path, monkeypatch):
        user_profiles.ensure_profile(db_path, "alice")
        cfg = _config(db_path, {"alice": UserConfig(display_name="Alice")})
        assert refresh_user_profiles_if_changed(cfg, min_interval=60.0) is True
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["alice@example.com"],
        )
        assert refresh_user_profiles_if_changed(cfg, min_interval=60.0) is False
        assert cfg.find_user_by_email("alice@example.com") is None
        assert refresh_user_profiles_if_changed(cfg) is True

    def test_a_missing_database_is_quietly_false(self, tmp_path):
        cfg = _config(tmp_path / "absent.db")
        assert refresh_user_profiles_if_changed(cfg) is False
        assert not (tmp_path / "absent.db").exists()


class TestTheMigration:
    def _declared(self, tmp_path) -> Path:
        path = tmp_path / "declared.db"
        raw = sqlite3.connect(path)
        raw.executescript(SCHEMA.read_text())
        raw.close()
        return path

    def _objects(self, path) -> dict[str, str]:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE name = 'profile_generation' OR name IN (?, ?, ?)",
                TRIGGERS,
            ).fetchall()
        return {name: " ".join(sql.split()) for name, sql in rows}

    def test_the_migration_builds_what_schema_sql_declares(self, tmp_path):
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        for trigger in TRIGGERS:
            raw.execute(f"DROP TRIGGER {trigger}")
        raw.execute("DROP TABLE profile_generation")
        raw.commit()
        raw.row_factory = sqlite3.Row
        db._run_migrations(raw)
        raw.commit()
        raw.close()

        upgraded = self._objects(old)
        assert set(upgraded) == {"profile_generation", *TRIGGERS}
        assert upgraded == self._objects(self._declared(tmp_path))
        assert _generation(old) == 0

    def test_a_database_without_user_profiles_skips_the_triggers(self, tmp_path):
        """On a fresh install the migrations run before `schema.sql` creates
        `user_profiles`; the table is made and the triggers wait for it."""
        conn = sqlite3.connect(tmp_path / "bare.db")
        db._migrate_profile_generation(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert "profile_generation" in names
        assert not names & set(TRIGGERS)

    def test_schema_sql_and_the_migration_carry_the_same_ddl(self):
        squash = lambda s: re.sub(r"--[^\n]*", "", s)  # noqa: E731
        normal = lambda s: " ".join(squash(s).split()).rstrip(";")  # noqa: E731
        flat = " ".join(squash(SCHEMA.read_text()).split())
        for statement in (
            *db._PROFILE_GENERATION_TABLE_DDL, *db._PROFILE_GENERATION_TRIGGER_DDL,
        ):
            assert normal(statement) in flat, statement.split("(")[0]


class TestTheCallers:
    def test_the_web_auth_dependency_refreshes_first(self, db_path, monkeypatch):
        """`_require_api_auth` checks before the session read, so even a
        request it then refuses has brought the snapshot up to date."""
        from istota.webui import app as web_app

        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["alice@example.com"],
        )
        cfg = _config(db_path)
        monkeypatch.setattr(web_app, "_config", cfg)
        request = SimpleNamespace(session={})
        with pytest.raises(web_app._UnauthorizedException):
            web_app._require_api_auth(request)
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_the_scheduler_loop_picks_up_a_write_without_a_restart(
        self, tmp_path, monkeypatch,
    ):
        import istota.scheduler as sched
        from tests.test_background_checks import _daemon_config, _run_daemon_isolated

        cfg = _daemon_config(tmp_path)
        wrote = threading.Event()
        resolved: list[str | None] = []

        def _dispatch(self):
            if not wrote.is_set():
                user_profiles.update_profile(
                    cfg.db_path, "bob", email_addresses=["bob@example.com"],
                )
                wrote.set()
                return
            found = cfg.find_user_by_email("bob@example.com")
            if found is not None:
                resolved.append(found)
                sched.request_shutdown()

        t = _run_daemon_isolated(cfg, monkeypatch, _dispatch)
        try:
            t.join(timeout=15.0)
            assert not t.is_alive(), "the loop never resolved the new address"
            assert resolved == ["bob"]
        finally:
            sched.request_shutdown()
            t.join(timeout=10.0)
