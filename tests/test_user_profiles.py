"""Tests for the user_profiles store (Phase 6)."""

from __future__ import annotations

import sqlite3

import pytest

from istota import user_profiles
from istota.config import UserConfig
from istota.user_profiles import UserProfile


class TestEnsureAndGet:
    def test_ensure_creates_row(self, db_path):
        profile = user_profiles.ensure_profile(db_path, "alice", display_name="Alice", timezone="UTC")
        assert profile.user_id == "alice"
        assert profile.display_name == "Alice"
        assert profile.timezone == "UTC"

    def test_ensure_is_idempotent(self, db_path):
        a = user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        b = user_profiles.ensure_profile(db_path, "alice", display_name="Bob")
        # Existing display_name preserved on second call
        assert a.display_name == "Alice"
        assert b.display_name == "Alice"

    def test_get_missing_returns_none(self, db_path):
        assert user_profiles.get_profile(db_path, "ghost") is None

    def test_seed_defaults_when_unspecified(self, db_path):
        profile = user_profiles.ensure_profile(db_path, "alice")
        assert profile.display_name == "alice"
        assert profile.timezone == "UTC"
        assert profile.email_addresses == []
        assert profile.disabled_skills == []
        assert profile.max_foreground_workers == 0


class TestUpdate:
    def test_partial_update(self, db_path):
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        updated = user_profiles.update_profile(db_path, "alice", timezone="America/Los_Angeles")
        assert updated.timezone == "America/Los_Angeles"
        assert updated.display_name == "Alice"  # untouched

    def test_update_lists(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(
            db_path, "alice",
            email_addresses=["a@x.com", "b@x.com"],
            trusted_email_senders=["*@trusted.org"],
            disabled_skills=["bookmarks"],
            disabled_modules=["feeds"],
        )
        p = user_profiles.get_profile(db_path, "alice")
        assert p.email_addresses == ["a@x.com", "b@x.com"]
        assert p.trusted_email_senders == ["*@trusted.org"]
        assert p.disabled_skills == ["bookmarks"]
        assert p.disabled_modules == ["feeds"]

    def test_update_unknown_field_raises(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        with pytest.raises(ValueError, match="unknown profile field"):
            user_profiles.update_profile(db_path, "alice", evil="🪤")

    def test_update_missing_user_raises(self, db_path):
        with pytest.raises(ValueError, match="no user_profile row"):
            user_profiles.update_profile(db_path, "ghost", timezone="UTC")

    def test_site_enabled_coercion(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(db_path, "alice", site_enabled=True)
        assert user_profiles.get_profile(db_path, "alice").site_enabled is True
        user_profiles.update_profile(db_path, "alice", site_enabled=False)
        assert user_profiles.get_profile(db_path, "alice").site_enabled is False

    def test_updated_at_bumps(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        with sqlite3.connect(db_path) as conn:
            t1 = conn.execute("SELECT updated_at FROM user_profiles WHERE user_id='alice'").fetchone()[0]
        user_profiles.update_profile(db_path, "alice", timezone="Europe/London")
        with sqlite3.connect(db_path) as conn:
            t2 = conn.execute("SELECT updated_at FROM user_profiles WHERE user_id='alice'").fetchone()[0]
        assert t1 is not None and t2 is not None  # both populated


class TestUpsert:
    def test_upsert_overwrites_all_fields(self, db_path):
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        user_profiles.upsert_profile(db_path, UserProfile(
            user_id="alice",
            display_name="Alice the Second",
            timezone="UTC",
            email_addresses=["alice@x.com"],
            log_channel="abc123",
            site_enabled=True,
        ))
        p = user_profiles.get_profile(db_path, "alice")
        assert p.display_name == "Alice the Second"
        assert p.email_addresses == ["alice@x.com"]
        assert p.log_channel == "abc123"
        assert p.site_enabled is True


class TestDelete:
    def test_delete_existing(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        assert user_profiles.delete_profile(db_path, "alice") is True
        assert user_profiles.get_profile(db_path, "alice") is None

    def test_delete_missing(self, db_path):
        assert user_profiles.delete_profile(db_path, "ghost") is False


class TestList:
    def test_list_empty(self, db_path):
        assert user_profiles.list_profiles(db_path) == {}

    def test_list_multiple(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.ensure_profile(db_path, "bob")
        rows = user_profiles.list_profiles(db_path)
        assert set(rows) == {"alice", "bob"}


class TestImportFromUserConfigs:
    def test_imports_missing_rows(self, db_path):
        user_configs = {
            "alice": UserConfig(
                display_name="Alice",
                email_addresses=["alice@x.com"],
                timezone="America/New_York",
                trusted_email_senders=["*@trusted.org"],
                log_channel="LOG", alerts_channel="ALR",
            ),
        }
        written = user_profiles.import_from_user_configs(db_path, user_configs)
        assert written == 1
        p = user_profiles.get_profile(db_path, "alice")
        assert p.display_name == "Alice"
        assert p.email_addresses == ["alice@x.com"]
        assert p.timezone == "America/New_York"
        assert p.trusted_email_senders == ["*@trusted.org"]
        assert p.log_channel == "LOG"
        assert p.alerts_channel == "ALR"

    def test_does_not_overwrite_existing(self, db_path):
        # User already has a row from web UI edit
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice DB", timezone="UTC")
        user_profiles.update_profile(db_path, "alice", display_name="Alice From Web")

        # TOML re-import should NOT clobber the web UI value
        user_configs = {"alice": UserConfig(display_name="Alice TOML", timezone="UTC")}
        written = user_profiles.import_from_user_configs(db_path, user_configs)
        assert written == 0
        assert user_profiles.get_profile(db_path, "alice").display_name == "Alice From Web"

    def test_idempotent(self, db_path):
        user_configs = {"alice": UserConfig(display_name="Alice")}
        first = user_profiles.import_from_user_configs(db_path, user_configs)
        second = user_profiles.import_from_user_configs(db_path, user_configs)
        assert first == 1
        assert second == 0


class TestMergeIntoUserConfig:
    def test_db_wins_for_scalars(self):
        uc = UserConfig(display_name="Alice TOML", timezone="UTC", log_channel="OLD")
        profile = UserProfile(
            user_id="alice", display_name="Alice DB",
            timezone="Europe/London", log_channel="NEW",
        )
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.display_name == "Alice DB"
        assert uc.timezone == "Europe/London"
        assert uc.log_channel == "NEW"

    def test_empty_db_lists_replace_toml_lists(self):
        # Mulder P0 fix: once a row exists, the DB owns the list fields,
        # period. Empty DB list = user explicitly cleared it. The
        # ensure_profile path seeds TOML lists into the row at creation
        # time, so an empty DB list always means "intentionally empty".
        uc = UserConfig(email_addresses=["a@x.com"], disabled_skills=["foo"])
        profile = UserProfile(user_id="alice")  # all empty defaults
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.email_addresses == []
        assert uc.disabled_skills == []

    def test_db_lists_replace_toml_when_set(self):
        uc = UserConfig(email_addresses=["a@x.com"])
        profile = UserProfile(user_id="alice", email_addresses=["b@x.com", "c@x.com"])
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.email_addresses == ["b@x.com", "c@x.com"]


class TestEnsureSeedFromTomlConfig:
    """Mulder P0/P1 fix: ensure_profile must seed list fields from the
    full TOML UserConfig when a row is created via auto-seed (web callback),
    so an empty DB list later means 'user cleared it' rather than 'row
    not yet populated'."""

    def test_auto_seed_carries_full_toml_payload(self, db_path):
        toml_uc = UserConfig(
            display_name="Alice TOML",
            email_addresses=["a@x.com", "b@x.com"],
            trusted_email_senders=["*@trust.org"],
            timezone="Europe/Berlin",
            log_channel="LOG-T",
            disabled_skills=["bookmarks"],
            max_foreground_workers=4,
        )
        profile = user_profiles.ensure_profile(
            db_path, "alice",
            display_name="Alice from NC",  # different from TOML
            seed_from=toml_uc,
        )
        # display_name uses the explicit param (NC display name)
        assert profile.display_name == "Alice from NC"
        # but every other list/scalar field comes from TOML
        assert profile.email_addresses == ["a@x.com", "b@x.com"]
        assert profile.trusted_email_senders == ["*@trust.org"]
        assert profile.timezone == "Europe/Berlin"
        assert profile.log_channel == "LOG-T"
        assert profile.disabled_skills == ["bookmarks"]
        assert profile.max_foreground_workers == 4

    def test_existing_row_not_reseeded(self, db_path):
        # Pre-existing row with a deliberately-cleared email list
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        user_profiles.update_profile(db_path, "alice", email_addresses=[])

        # Subsequent auto-seed pass with TOML emails must NOT reseed
        toml_uc = UserConfig(email_addresses=["should-not-appear@x.com"])
        profile = user_profiles.ensure_profile(
            db_path, "alice", display_name="Alice", seed_from=toml_uc,
        )
        assert profile.email_addresses == []

    def test_status_returns_created_flag(self, db_path):
        _, created_first = user_profiles.ensure_profile_with_status(
            db_path, "alice", display_name="Alice",
        )
        assert created_first is True
        _, created_second = user_profiles.ensure_profile_with_status(
            db_path, "alice", display_name="Alice Again",
        )
        assert created_second is False


class TestMergeOwnsListsAfterRowExists:
    """Once a DB row exists, an empty list means the user cleared it.
    The merge must not resurrect TOML lists in that case."""

    def test_empty_db_list_overrides_toml_list(self):
        uc = UserConfig(email_addresses=["should-not-appear@x.com"])
        # Row exists with cleared lists (representing user explicitly clearing).
        profile = UserProfile(user_id="alice", email_addresses=[])
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.email_addresses == []


class TestJsonDecodeResilience:
    def test_corrupt_json_falls_back_to_empty(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        # Write garbage directly into the email_addresses column.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE user_profiles SET email_addresses = 'not-json' WHERE user_id = 'alice'"
            )
        p = user_profiles.get_profile(db_path, "alice")
        assert p is not None
        assert p.email_addresses == []  # decoded as empty rather than raising


class TestRoutingFields:
    """routing (dict) + default_destination (scalar) round-trip and merge."""

    def test_defaults_on_fresh_row(self, db_path):
        p = user_profiles.ensure_profile(db_path, "alice")
        assert p.routing == {}
        assert p.default_destination == "talk"

    def test_routing_dict_round_trip(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        p = user_profiles.update_profile(
            db_path, "alice",
            routing={"alert": "email", "briefing": "talk:room"},
            default_destination="both",
        )
        assert p.routing == {"alert": "email", "briefing": "talk:room"}
        assert p.default_destination == "both"
        # re-read from disk
        again = user_profiles.get_profile(db_path, "alice")
        assert again.routing == {"alert": "email", "briefing": "talk:room"}
        assert again.default_destination == "both"

    def test_routing_noop_detection(self, db_path):
        user_profiles.update_profile_with_status(
            db_path, "alice", routing={"alert": "email"},
        )
        _, state = user_profiles.update_profile_with_status(
            db_path, "alice", routing={"alert": "email"},
        )
        assert state == "noop"

    def test_default_destination_empty_coerces_to_talk(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        p = user_profiles.update_profile(db_path, "alice", default_destination="")
        assert p.default_destination == "talk"

    def test_merge_routing_db_owns_it(self):
        uc = UserConfig(routing={"alert": "talk"}, default_destination="talk")
        profile = UserProfile(
            user_id="alice", routing={"briefing": "email"}, default_destination="both",
        )
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.routing == {"briefing": "email"}
        assert uc.default_destination == "both"

    def test_migration_adds_columns(self, tmp_path):
        # A DB created fresh has the columns (schema.sql parity); also verify
        # the values are readable defaults.
        from istota import db as _db
        path = tmp_path / "fresh.db"
        _db.init_db(path)
        with _db.get_db(path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(user_profiles)")}
        assert "routing" in cols
        assert "default_destination" in cols
        assert "email_reply_routing" in cols


class TestEmailReplyRouting:
    """email_reply_routing scalar round-trip + merge (default origin+thread)."""

    def test_default_on_fresh_row(self, db_path):
        p = user_profiles.ensure_profile(db_path, "alice")
        assert p.email_reply_routing == "origin+thread"

    def test_round_trip(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        p = user_profiles.update_profile(
            db_path, "alice", email_reply_routing="origin",
        )
        assert p.email_reply_routing == "origin"
        again = user_profiles.get_profile(db_path, "alice")
        assert again.email_reply_routing == "origin"

    def test_empty_coerces_to_default(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        p = user_profiles.update_profile(db_path, "alice", email_reply_routing="")
        assert p.email_reply_routing == "origin+thread"

    def test_noop_detection(self, db_path):
        user_profiles.update_profile_with_status(
            db_path, "alice", email_reply_routing="thread",
        )
        _, state = user_profiles.update_profile_with_status(
            db_path, "alice", email_reply_routing="thread",
        )
        assert state == "noop"

    def test_merge_db_owns_it(self):
        uc = UserConfig()
        profile = UserProfile(user_id="alice", email_reply_routing="origin")
        user_profiles.merge_into_user_config(profile, uc)
        assert uc.email_reply_routing == "origin"

    def test_import_from_toml_seeds_value(self, db_path):
        uc = UserConfig(email_reply_routing="thread")
        n = user_profiles.import_from_user_configs(db_path, {"alice": uc})
        assert n == 1
        p = user_profiles.get_profile(db_path, "alice")
        assert p.email_reply_routing == "thread"
