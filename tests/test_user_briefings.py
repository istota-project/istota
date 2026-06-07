"""Tests for the user_briefings store (Phase 7b)."""

from __future__ import annotations

import pytest

from istota import user_briefings
from istota.config import BriefingConfig, UserConfig


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
        cfg.briefings = [
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
        cfg.briefings = [
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
        cfg.briefings = [
            BriefingConfig(name="m", cron="0 7 * * *",
                           conversation_token="t", output="talk", components={}),
        ]
        user_briefings.import_from_user_configs(db_path, {"alice": cfg})
        rows = user_briefings.list_briefings(db_path, "alice")
        assert len(rows) == 1
        assert rows[0].cron == "0 8 * * *"  # preserved
