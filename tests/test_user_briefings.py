"""Tests for the user_briefings store (Phase 7b)."""

from __future__ import annotations

import pytest

from istota import user_briefings
from istota.config import BriefingConfig, Config, UserConfig


class TestEnsureBriefing:
    def test_creates_row(self, db_path):
        b, state = user_briefings.ensure_briefing(
            db_path,
            user_id="alice",
            name="morning",
            cron="0 7 * * 1-5",
            conversation_token="tok123",
            output="talk",
            components={"calendar": True, "email": True},
        )
        assert state == "created"
        assert b.name == "morning"
        assert b.cron == "0 7 * * 1-5"
        assert b.output == "talk"
        assert b.components == {"calendar": True, "email": True}
        assert b.enabled is True

    def test_idempotent_second_call_is_noop(self, db_path):
        kwargs = dict(
            user_id="alice", name="morning", cron="0 7 * * 1-5",
            conversation_token="tok123", output="talk",
            components={"calendar": True},
        )
        user_briefings.ensure_briefing(db_path, **kwargs)
        _, state = user_briefings.ensure_briefing(db_path, **kwargs)
        assert state == "noop"

    def test_changing_cron_updates(self, db_path):
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="t", output="talk",
        )
        b, state = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 8 * * *", conversation_token="t", output="talk",
        )
        assert state == "updated"
        assert b.cron == "0 8 * * *"

    def test_disabled_briefing_persists_enabled_false(self, db_path):
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="evening",
            cron="0 19 * * *", conversation_token="t", output="talk",
            enabled=False,
        )
        assert b.enabled is False

    def test_email_output_does_not_require_token(self, db_path):
        # The store-level validator is independent of CLI / API checks.
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="weekly",
            cron="0 9 * * 1", output="email", components={},
        )
        assert b.conversation_token == ""
        assert b.output == "email"

    def test_empty_output_rejected(self, db_path):
        # An output that parses to no destinations (empty / "none") is invalid.
        for bad in ("", "none"):
            with pytest.raises(ValueError):
                user_briefings.ensure_briefing(
                    db_path, user_id="alice", name="x", cron="0 9 * * *",
                    output=bad,
                )

    def test_descriptor_output_accepted(self, db_path):
        # Descriptors (incl. comma lists and explicit channels) are accepted;
        # unknown surfaces are warn-and-dropped at delivery, not rejected here.
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="multi",
            cron="0 9 * * *", output="email,ntfy", components={},
        )
        assert b.output == "email,ntfy"

    def test_empty_name_rejected(self, db_path):
        with pytest.raises(ValueError):
            user_briefings.ensure_briefing(
                db_path, user_id="alice", name="", cron="0 9 * * *",
                conversation_token="t",
            )


class TestAdminHold:
    def _row(self, db_path, name="morning", user_id="alice", enabled=True):
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id=user_id, name=name, cron="0 7 * * *",
            conversation_token="t", output="talk", enabled=enabled,
        )
        return b

    def _task(self, db_path, user_id, name, status):
        from istota import db
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="p", user_id=user_id, source_type="briefing",
                briefing_name=name,
            )
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        return task_id

    def _status(self, db_path, task_id):
        from istota import db
        with db.get_db(db_path) as conn:
            return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def test_set_hold_records_who_and_when(self, db_path):
        b = self._row(db_path)
        held = user_briefings.set_hold(db_path, b.id, admin_id="root")
        assert held.admin_hold_by == "root"
        assert held.admin_hold_at
        assert held.enabled is True  # the owner's choice is untouched

    def test_set_hold_is_idempotent_and_keeps_the_first_time(self, db_path):
        b = self._row(db_path)
        first = user_briefings.set_hold(db_path, b.id, admin_id="root")
        again = user_briefings.set_hold(db_path, b.id, admin_id="other")
        assert (again.admin_hold_at, again.admin_hold_by) == (first.admin_hold_at, "root")

    def test_unknown_id_returns_none(self, db_path):
        assert user_briefings.set_hold(db_path, 999, admin_id="root") is None
        assert user_briefings.clear_hold(db_path, 999) is None

    def test_clear_hold_restores_the_owners_enabled(self, db_path):
        b = self._row(db_path, enabled=False)
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        cleared = user_briefings.clear_hold(db_path, b.id)
        assert (cleared.admin_hold_at, cleared.admin_hold_by) == (None, None)
        assert cleared.enabled is False

    def test_owner_save_leaves_the_hold(self, db_path):
        b = self._row(db_path, enabled=False)
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        saved, state = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 8 * * *",
            conversation_token="t", output="talk", enabled=True,
        )
        assert state == "updated"
        assert saved.enabled is True
        assert saved.admin_hold_by == "root"

    def test_hold_cancels_pending_tasks_of_that_briefing_only(self, db_path):
        b = self._row(db_path)
        self._row(db_path, name="evening")
        pending = self._task(db_path, "alice", "morning", "pending")
        running = self._task(db_path, "alice", "morning", "running")
        done = self._task(db_path, "alice", "morning", "completed")
        other = self._task(db_path, "alice", "evening", "pending")
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        assert self._status(db_path, pending) == "cancelled"
        assert self._status(db_path, running) == "running"
        assert self._status(db_path, done) == "completed"
        assert self._status(db_path, other) == "pending"

    def test_create_task_refuses_a_held_briefing(self, db_path):
        from istota import db
        b = self._row(db_path)
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        with db.get_db(db_path) as conn, pytest.raises(ValueError, match="briefing_held"):
            db.create_task(conn, prompt="p", user_id="alice", source_type="briefing",
                           briefing_name="morning")
        user_briefings.clear_hold(db_path, b.id)
        assert self._task(db_path, "alice", "morning", "pending")

    def test_overlay_keeps_a_held_row(self, db_path):
        # create_task refuses it, so a daemon that booted during the hold can
        # still run it once the hold is lifted.
        from istota import config as cfgmod
        b = self._row(db_path)
        self._row(db_path, name="evening")
        self._row(db_path, name="off", enabled=False)
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        cfg = Config(db_path=db_path)
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        cfgmod._apply_user_briefings(cfg)
        assert sorted(x.name for x in cfg.users["alice"].briefings) == ["evening", "morning"]

    def test_hold_leaves_failed_history_alone(self, db_path):
        from istota import db
        b = self._row(db_path)
        old = self._task(db_path, "alice", "morning", "failed")
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET error = 'boom', created_at = datetime('now', '-30 days') "
                         "WHERE id = ?", (old,))
        user_briefings.set_hold(db_path, b.id, admin_id="root")
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT status, error FROM tasks WHERE id = ?", (old,)).fetchone()
        assert tuple(row) == ("failed", "boom")

    def test_create_task_refuses_a_deleted_briefing(self, db_path):
        from istota import db
        b = self._row(db_path)
        assert user_briefings.delete_briefing_by_id(db_path, "alice", b.id)
        with db.get_db(db_path) as conn, pytest.raises(ValueError, match="briefing_deleted"):
            db.create_task(conn, prompt="p", user_id="alice", source_type="briefing",
                           briefing_name="morning")

    def test_migration_adds_the_hold_columns(self, tmp_path):
        import sqlite3

        from istota import db

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE briefing_configs (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, "
            "name TEXT NOT NULL, cron_expression TEXT NOT NULL, conversation_token TEXT NOT NULL, "
            "components TEXT NOT NULL, enabled INTEGER DEFAULT 1, last_run_at TEXT, "
            "created_at TEXT DEFAULT (datetime('now')), UNIQUE(user_id, name))"
        )
        conn.execute(
            "INSERT INTO briefing_configs (user_id, name, cron_expression, conversation_token, "
            "components) VALUES ('alice', 'morning', '0 7 * * *', 't', '{}')"
        )
        conn.commit()
        conn.close()
        db.init_db(path)
        row = user_briefings.get_briefing(path, "alice", "morning")
        assert (row.admin_hold_at, row.admin_hold_by) == (None, None)
        assert user_briefings.set_hold(path, row.id, admin_id="root").admin_hold_by == "root"


class TestListAndDelete:
    def test_list_scopes_to_user(self, db_path):
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="m", cron="0 7 * * *",
            conversation_token="t",
        )
        user_briefings.ensure_briefing(
            db_path, user_id="bob", name="m", cron="0 9 * * *",
            conversation_token="t",
        )
        alice = user_briefings.list_briefings(db_path, "alice")
        assert [b.user_id for b in alice] == ["alice"]

    def test_delete_by_name(self, db_path):
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="m", cron="0 7 * * *",
            conversation_token="t",
        )
        assert user_briefings.delete_briefing(db_path, "alice", "m")
        assert not user_briefings.delete_briefing(db_path, "alice", "m")

    def test_delete_by_id_scoped_to_user(self, db_path):
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="m", cron="0 7 * * *",
            conversation_token="t",
        )
        # Bob cannot delete Alice's row by guessing the id.
        assert not user_briefings.delete_briefing_by_id(db_path, "bob", b.id)
        assert user_briefings.delete_briefing_by_id(db_path, "alice", b.id)


class TestImportFromUserConfigs:
    def test_seeds_rows_from_toml(self, db_path):
        cfg = UserConfig(display_name="Alice")
        cfg.legacy_briefing_seeds = [
            BriefingConfig(name="morning", cron="0 7 * * 1-5",
                           conversation_token="t", output="talk",
                           components={"calendar": True}),
            BriefingConfig(name="evening", cron="0 19 * * *",
                           conversation_token="t", output="email",
                           components={}),
        ]
        written = user_briefings.import_from_user_configs(
            db_path, {"alice": cfg},
        )
        assert written == 2
        names = {b.name for b in user_briefings.list_briefings(db_path, "alice")}
        assert names == {"morning", "evening"}

    def test_import_is_idempotent(self, db_path):
        cfg = UserConfig(display_name="Alice")
        cfg.legacy_briefing_seeds = [
            BriefingConfig(name="m", cron="0 7 * * *", conversation_token="t",
                           output="talk", components={}),
        ]
        user_briefings.import_from_user_configs(db_path, {"alice": cfg})
        # Second pass writes nothing — DB row exists.
        written = user_briefings.import_from_user_configs(db_path, {"alice": cfg})
        assert written == 0

    def test_import_does_not_overwrite_existing_db_row(self, db_path):
        # DB row wins. TOML migration only seeds, never updates.
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="m", cron="0 8 * * *",
            conversation_token="t", output="talk", components={},
        )
        cfg = UserConfig(display_name="Alice")
        cfg.legacy_briefing_seeds = [
            BriefingConfig(name="m", cron="0 7 * * *",
                           conversation_token="t", output="talk", components={}),
        ]
        user_briefings.import_from_user_configs(db_path, {"alice": cfg})
        rows = user_briefings.list_briefings(db_path, "alice")
        assert len(rows) == 1
        assert rows[0].cron == "0 8 * * *"  # preserved


class TestOutputColumn:
    def test_output_stored_in_column_not_components(self, db_path):
        """The real ``output`` column carries delivery; components has no __output__."""
        import sqlite3

        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="m", cron="0 7 * * *",
            conversation_token="t", output="email,ntfy",
            components={"calendar": True},
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT output, components FROM briefing_configs WHERE name='m'"
        ).fetchone()
        conn.close()
        assert row["output"] == "email,ntfy"
        assert "__output__" not in row["components"]

    def test_legacy_output_backfilled_by_migration(self, db_path):
        """A legacy row with __output__ packed in components is migrated to the column."""
        import json
        import sqlite3

        from istota import db

        # Simulate a pre-migration row: output smuggled into components JSON.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO briefing_configs
                (user_id, name, cron_expression, conversation_token, components, output, enabled)
            VALUES ('alice', 'legacy', '0 7 * * *', 't', ?, 'talk', 1)
            """,
            (json.dumps({"calendar": True, "__output__": "email"}),),
        )
        conn.commit()
        conn.close()

        # Re-run migrations (idempotent) to trigger the backfill.
        db.init_db(db_path)

        b = user_briefings.get_briefing(db_path, "alice", "legacy")
        assert b is not None
        assert b.output == "email"
        assert b.components == {"calendar": True}
        assert "__output__" not in b.components

    def test_backfill_is_idempotent(self, db_path):
        import json
        import sqlite3

        from istota import db

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO briefing_configs
                (user_id, name, cron_expression, conversation_token, components, output, enabled)
            VALUES ('alice', 'legacy', '0 7 * * *', 't', ?, 'talk', 1)
            """,
            (json.dumps({"__output__": "both"}),),
        )
        conn.commit()
        conn.close()

        db.init_db(db_path)
        db.init_db(db_path)  # second run is a no-op

        b = user_briefings.get_briefing(db_path, "alice", "legacy")
        assert b is not None
        assert b.output == "both"
        assert b.components == {}


class TestDefaultBriefings:
    """Legacy defaults no longer copy into new personal schedules."""

    def _default(self, name="Daily"):
        return BriefingConfig(
            name=name, cron="0 7 * * *", output="talk",
            blocks=[{"title": "News", "sources": [{"kind": "rss", "config": {}}]}],
        )

    def test_new_user_does_not_get_copied_defaults(self, db_path):
        from istota import config as cfgmod

        cfg = Config(db_path=db_path, default_briefings=[self._default()])
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        cfgmod._apply_user_briefings(cfg)
        briefings = cfg.users["alice"].briefings
        assert briefings == []

    def test_opted_out_user_gets_none(self, db_path):
        from istota import config as cfgmod

        cfg = Config(db_path=db_path, default_briefings=[self._default()])
        cfg.users = {"bob": UserConfig(display_name="Bob", default_briefings=False)}
        cfgmod._apply_user_briefings(cfg)
        assert cfg.users["bob"].briefings == []

    def test_user_own_briefing_wins_by_name(self, db_path):
        from istota import config as cfgmod

        user_briefings.ensure_briefing(
            db_path, user_id="carol", name="Daily", cron="0 9 * * *", output="email",
        )
        cfg = Config(db_path=db_path, default_briefings=[self._default()])
        cfg.users = {"carol": UserConfig(display_name="Carol")}
        cfgmod._apply_user_briefings(cfg)
        daily = [b for b in cfg.users["carol"].briefings if b.name == "Daily"]
        assert len(daily) == 1  # no duplicate
        assert daily[0].cron == "0 9 * * *"  # user's own, default not merged

    def test_personal_import_does_not_copy_defaults(self, db_path):
        from istota import config as cfgmod

        cfg = Config(db_path=db_path, default_briefings=[self._default()])
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        cfgmod._apply_user_briefings(cfg)
        # The importer only sees explicit personal schedules.
        written = user_briefings.import_from_user_configs(db_path, cfg.users)
        assert written == 0
        assert user_briefings.list_briefings(db_path, "alice") == []

    def test_no_defaults_configured_is_noop(self, db_path):
        from istota import config as cfgmod

        cfg = Config(db_path=db_path, default_briefings=[])
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        cfgmod._apply_user_briefings(cfg)
        assert cfg.users["alice"].briefings == []
