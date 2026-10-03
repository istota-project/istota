"""`istota user ensure --managed / --seed` and the `user_profile_managed_fields` table.

A field the deploy asserts on every converge is re-written by the next
`user ensure`, so an edit made in the web UI in between is reverted silently.
`--managed` records what the call asserted, and the web refuses to edit those
fields. `--seed` hands ownership to the web UI: it writes a new user in full,
fills only an empty SMS number or an unbound WhatsApp binding on an existing
one, and releases every lock.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

from istota import db, user_profiles
from tests.test_cli_user_ensure import _FakeArgs, cfg_with_db  # noqa: F401

SCHEMA = Path(db.__file__).resolve().parent.parent.parent / "schema.sql"


def _ensure(cfg, capsys=None, **kwargs) -> str:
    from istota.cli import cmd_user_ensure

    cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", **kwargs))
    if capsys is None:
        return ""
    return capsys.readouterr().out


def _managed(db_path, user_id="alice") -> set[str]:
    with db.get_db(db_path) as conn:
        return user_profiles.managed_fields(conn, user_id)


def _binding(db_path, user_id="alice"):
    with db.get_db(db_path) as conn:
        return db.get_whatsapp_binding(conn, user_id)


class TestManaged:
    def test_records_exactly_the_written_fields_and_releases_the_rest(
        self, cfg_with_db,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(
            cfg, managed=True,
            email=["alice@example.com"], trusted_sender=["*@example.com"],
            max_foreground_workers=2,
        )
        assert _managed(db_path) == {
            "email_addresses", "trusted_email_senders", "max_foreground_workers",
        }

        _ensure(cfg, managed=True, email=["alice@example.com"])
        assert _managed(db_path) == {"email_addresses"}

    def test_a_clear_counts_as_writing_the_field(self, cfg_with_db):  # noqa: F811
        cfg, db_path = cfg_with_db
        _ensure(cfg, managed=True, clear_sms_number=True, clear_whatsapp=True)
        assert _managed(db_path) == {"sms_phone_number", "whatsapp_number"}

    def test_a_whatsapp_number_is_marked_under_its_own_name(
        self, cfg_with_db,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(cfg, managed=True, whatsapp_number="+15551234567")
        assert _managed(db_path) == {"whatsapp_number"}

    def test_the_managed_set_alone_does_not_move_state(
        self, cfg_with_db, capsys,  # noqa: F811
    ):
        """The role's restart handlers key off STATE; the web reads the
        managed set live and needs no restart."""
        cfg, db_path = cfg_with_db
        _ensure(cfg, email=["alice@example.com"])
        capsys.readouterr()
        out = _ensure(cfg, capsys, managed=True, email=["alice@example.com"])
        assert "STATE: noop" in out
        assert _managed(db_path) == {"email_addresses"}

    def test_a_refusal_writes_neither_the_profile_nor_the_managed_set(
        self, cfg_with_db,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        user_profiles.ensure_profile(db_path, "bob")
        user_profiles.update_profile(
            db_path, "bob", email_addresses=["bob@example.com"],
        )
        _ensure(cfg, managed=True, display_name="Alice")
        with pytest.raises(SystemExit) as exc:
            _ensure(
                cfg, managed=True, display_name="Changed",
                email=["bob@example.com"],
            )
        assert exc.value.code == 1
        assert user_profiles.get_profile(db_path, "alice").display_name == "Alice"
        assert _managed(db_path) == {"display_name"}

    def test_a_rejected_whatsapp_number_rolls_the_profile_back(
        self, cfg_with_db,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        with pytest.raises(SystemExit):
            _ensure(
                cfg, managed=True, display_name="Alice",
                whatsapp_number="555-1234",
            )
        assert user_profiles.get_profile(db_path, "alice") is None
        assert _managed(db_path) == set()


class TestSeed:
    def test_a_new_user_is_written_in_full_and_marks_nothing(
        self, cfg_with_db, capsys,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        out = _ensure(
            cfg, capsys, seed=True,
            display_name="Alice", email=["alice@example.com"],
            sms_number="+15550001111", whatsapp_number="+15551234567",
        )
        assert "STATE: created" in out
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.email_addresses == ["alice@example.com"]
        assert profile.sms_phone_number == "+15550001111"
        assert _binding(db_path).bootstrap_phone_number == "+15551234567"
        assert _managed(db_path) == set()

    def test_an_existing_users_stored_address_is_kept(
        self, cfg_with_db, capsys,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(cfg, email=["edited-in-the-web@example.com"])
        capsys.readouterr()
        out = _ensure(cfg, capsys, seed=True, email=["inventory@example.com"])
        assert "STATE: noop" in out
        assert "kept the stored value of email_addresses" in out
        assert user_profiles.get_profile(db_path, "alice").email_addresses == [
            "edited-in-the-web@example.com",
        ]

    def test_an_empty_sms_number_is_filled_and_a_set_one_is_not(
        self, cfg_with_db, capsys,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(cfg, display_name="Alice")
        capsys.readouterr()
        out = _ensure(cfg, capsys, seed=True, sms_number="+15550001111")
        assert "STATE: updated" in out
        assert user_profiles.get_profile(db_path, "alice").sms_phone_number == "+15550001111"

        out = _ensure(cfg, capsys, seed=True, sms_number="+15550002222")
        assert "STATE: noop" in out
        assert user_profiles.get_profile(db_path, "alice").sms_phone_number == "+15550001111"

    def test_a_clear_never_seeds(self, cfg_with_db, capsys):  # noqa: F811
        cfg, db_path = cfg_with_db
        _ensure(cfg, sms_number="+15550001111", whatsapp_number="+15551234567")
        capsys.readouterr()
        out = _ensure(
            cfg, capsys, seed=True, clear_sms_number=True, clear_whatsapp=True,
        )
        assert "STATE: noop" in out
        assert user_profiles.get_profile(db_path, "alice").sms_phone_number == "+15550001111"
        assert _binding(db_path) is not None

    def test_an_unbound_whatsapp_is_filled_and_a_bound_one_is_not(
        self, cfg_with_db, capsys,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(cfg, display_name="Alice")
        capsys.readouterr()
        out = _ensure(cfg, capsys, seed=True, whatsapp_number="+15551234567")
        assert "STATE: updated" in out
        assert _binding(db_path).bootstrap_phone_number == "+15551234567"

        out = _ensure(cfg, capsys, seed=True, whatsapp_number="+15559999999")
        assert "STATE: noop" in out
        assert _binding(db_path).bootstrap_phone_number == "+15551234567"

    def test_seed_releases_every_managed_field(self, cfg_with_db):  # noqa: F811
        cfg, db_path = cfg_with_db
        _ensure(cfg, managed=True, email=["alice@example.com"], display_name="A")
        assert _managed(db_path) == {"email_addresses", "display_name"}
        _ensure(cfg, seed=True, email=["alice@example.com"])
        assert _managed(db_path) == set()


class TestNeitherFlag:
    def test_a_plain_ensure_leaves_the_managed_set_alone(
        self, cfg_with_db,  # noqa: F811
    ):
        cfg, db_path = cfg_with_db
        _ensure(cfg, managed=True, email=["alice@example.com"])
        _ensure(cfg, display_name="Alice", trusted_sender=["*@example.com"])
        assert _managed(db_path) == {"email_addresses"}


class TestTheFlagsTogether:
    def test_the_parser_refuses_both(self, cfg_with_db, monkeypatch):  # noqa: F811
        import istota.cli as cli

        cfg, _ = cfg_with_db
        monkeypatch.setattr(sys, "argv", [
            "istota", "-c", str(cfg), "user", "ensure", "--name", "alice",
            "--managed", "--seed",
        ])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2

    def test_a_hand_built_call_is_refused_too(self, cfg_with_db):  # noqa: F811
        cfg, db_path = cfg_with_db
        with pytest.raises(SystemExit) as exc:
            _ensure(cfg, managed=True, seed=True, display_name="Alice")
        assert exc.value.code == 2
        assert user_profiles.get_profile(db_path, "alice") is None


class TestRefusedManagedFields:
    def _mark(self, db_path, *fields):
        with db.get_db(db_path) as conn:
            user_profiles.set_managed_fields(conn, "alice", fields)

    def test_an_unchanged_value_passes_and_a_changed_one_is_named(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["a@example.com"],
            max_foreground_workers=2,
        )
        self._mark(db_path, "email_addresses", "max_foreground_workers")
        with db.get_db(db_path) as conn:
            assert user_profiles.refused_managed_fields(conn, "alice", {
                "email_addresses": ["a@example.com"],
                "max_foreground_workers": 2,
                "display_name": "anything",
            }) == []
            assert user_profiles.refused_managed_fields(conn, "alice", {
                "email_addresses": ["b@example.com"],
                "max_foreground_workers": 3,
            }) == ["email_addresses", "max_foreground_workers"]

    def test_the_whatsapp_number_compares_with_the_binding(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15551234567",
            )
        self._mark(db_path, "whatsapp_number")
        with db.get_db(db_path) as conn:
            assert user_profiles.refused_managed_fields(
                conn, "alice", {"whatsapp_number": "+15551234567"},
            ) == []
            assert user_profiles.refused_managed_fields(
                conn, "alice", {"whatsapp_number": ""},
            ) == ["whatsapp_number"]

    def test_deleting_the_profile_releases_its_locks(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        self._mark(db_path, "email_addresses")
        user_profiles.delete_profile(db_path, "alice")
        assert _managed(db_path) == set()

    def test_an_unknown_field_cannot_be_marked(self, db_path):
        with db.get_db(db_path) as conn, pytest.raises(ValueError):
            user_profiles.set_managed_fields(conn, "alice", ["not_a_column"])


class TestTheMigration:
    TABLE = "user_profile_managed_fields"

    def _sql(self, path) -> str:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (self.TABLE,),
            ).fetchone()
        return " ".join(row[0].split()) if row else ""

    def test_the_migration_builds_what_schema_sql_declares(self, tmp_path):
        declared = tmp_path / "declared.db"
        raw = sqlite3.connect(declared)
        raw.executescript(SCHEMA.read_text())
        raw.close()

        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute(f"DROP TABLE {self.TABLE}")
        raw.commit()
        db._migrate_user_profile_managed_fields(raw)
        db._migrate_user_profile_managed_fields(raw)  # idempotent
        raw.commit()
        raw.close()

        assert self._sql(old)
        assert self._sql(old) == self._sql(declared)

    def test_schema_sql_and_the_migration_carry_the_same_ddl(self):
        squash = lambda s: re.sub(r"--[^\n]*", "", s)  # noqa: E731
        flat = " ".join(squash(SCHEMA.read_text()).split())
        statement = " ".join(squash(db._USER_PROFILE_MANAGED_FIELDS_DDL).split())
        assert statement in flat

    def test_a_database_without_the_table_reads_as_nothing_managed(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute(f"DROP TABLE {self.TABLE}")
        with db.get_db(db_path) as conn:
            assert user_profiles.managed_fields(conn, "alice") == set()
            assert user_profiles.set_managed_fields(conn, "alice", ()) is False
            assert user_profiles.refused_managed_fields(
                conn, "alice", {"email_addresses": ["x@example.com"]},
            ) == []
