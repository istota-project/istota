"""Tests for the encrypted secrets store (Phase 5)."""

from __future__ import annotations

import os
import sqlite3
from unittest import mock

import pytest

from istota import secrets_store
from istota.config import ResourceConfig, UserConfig


@pytest.fixture
def secret_key_env():
    """Set ISTOTA_SECRET_KEY for the duration of a test."""
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


class TestRoundTrip:
    def test_set_and_get(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "alice@example.com")
        got = secrets_store.get_secret(db_path, "alice", "monarch", "email")
        assert got == "alice@example.com"

    def test_overwrite_updates_value(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "password", "old")
        secrets_store.set_secret(db_path, "alice", "monarch", "password", "new")
        assert secrets_store.get_secret(db_path, "alice", "monarch", "password") == "new"

    def test_empty_value_deletes(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "k")
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "")
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") is None

    def test_per_user_scoping(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "alice@x.com")
        secrets_store.set_secret(db_path, "bob", "monarch", "email", "bob@x.com")
        assert secrets_store.get_secret(db_path, "alice", "monarch", "email") == "alice@x.com"
        assert secrets_store.get_secret(db_path, "bob", "monarch", "email") == "bob@x.com"

    def test_delete_returns_true_when_present(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "password", "p")
        assert secrets_store.delete_secret(db_path, "alice", "monarch", "password") is True
        assert secrets_store.delete_secret(db_path, "alice", "monarch", "password") is False

    def test_get_nonexistent_returns_none(self, db_path, secret_key_env):
        assert secrets_store.get_secret(db_path, "alice", "monarch", "email") is None


class TestEncryption:
    def test_encrypted_at_rest(self, db_path, secret_key_env):
        """Plaintext value must not appear anywhere in the encrypted_value column."""
        secret = "hunter2-passphrase"
        secrets_store.set_secret(db_path, "alice", "monarch", "password", secret)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_value FROM secrets "
                "WHERE user_id=? AND service=? AND key=?",
                ("alice", "monarch", "password"),
            ).fetchone()
        assert row is not None
        # ciphertext is bytes — never the plaintext
        assert isinstance(row[0], (bytes, memoryview))
        assert secret.encode("utf-8") not in bytes(row[0])

    def test_decrypt_fails_with_different_key(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "password", "p")
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "different-key" * 4}):
            assert secrets_store.get_secret(db_path, "alice", "monarch", "password") is None


class TestKeyMissing:
    def test_set_without_key_raises(self, db_path):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOTA_SECRET_KEY", None)
            with pytest.raises(secrets_store.SecretKeyMissingError):
                secrets_store.set_secret(db_path, "alice", "monarch", "email", "x")

    def test_get_without_key_returns_none(self, db_path):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOTA_SECRET_KEY", None)
            assert secrets_store.get_secret(db_path, "alice", "monarch", "email") is None

    def test_secret_key_available_reflects_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOTA_SECRET_KEY", None)
            assert secrets_store.secret_key_available() is False
            # Single-char keys are below the minimum length floor — not "available".
            os.environ["ISTOTA_SECRET_KEY"] = "x"
            assert secrets_store.secret_key_available() is False
            os.environ["ISTOTA_SECRET_KEY"] = "a" * 32
            assert secrets_store.secret_key_available() is True

    def test_set_with_too_short_key_raises(self, db_path):
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "tooshort"}):
            with pytest.raises(secrets_store.SecretKeyTooWeakError):
                secrets_store.set_secret(db_path, "alice", "monarch", "email", "x")

    def test_get_with_too_short_key_returns_none(self, db_path, secret_key_env):
        # Insert under a strong key, then check that a too-short key reads as None.
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "x@y")
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "weak"}):
            assert secrets_store.get_secret(db_path, "alice", "monarch", "email") is None


class TestListUserServices:
    def test_returns_metadata_only(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "x@y.com")
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "abc")
        listing = secrets_store.list_user_services(db_path, "alice")
        assert "monarch" in listing and "karakeep" in listing
        # plaintext never returned
        for service_entries in listing.values():
            for entry in service_entries:
                assert "value" not in entry
                assert "encrypted_value" not in entry

    def test_excludes_other_users(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "x")
        secrets_store.set_secret(db_path, "bob", "monarch", "email", "y")
        assert "monarch" in secrets_store.list_user_services(db_path, "alice")
        assert "monarch" in secrets_store.list_user_services(db_path, "bob")
        # Sanity: alice's listing should only have one entry, not bob's.
        assert len(secrets_store.list_user_services(db_path, "alice")["monarch"]) == 1


class TestResolution:
    def test_secrets_table_wins(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "from-db")
        got = secrets_store.resolve_secret(
            db_path, "alice", "monarch", "email",
            fallback_extras={"email": "from-toml"},
            fallback_env="MONARCH_EMAIL",
        )
        assert got == "from-db"

    def test_falls_back_to_extras(self, db_path, secret_key_env):
        got = secrets_store.resolve_secret(
            db_path, "alice", "monarch", "email",
            fallback_extras={"email": "from-toml"},
        )
        assert got == "from-toml"

    def test_falls_back_to_env(self, db_path, secret_key_env):
        with mock.patch.dict(os.environ, {"MY_VAR": "from-env"}):
            got = secrets_store.resolve_secret(
                db_path, "alice", "monarch", "email", fallback_env="MY_VAR",
            )
            assert got == "from-env"

    def test_returns_none_when_nothing_set(self, db_path, secret_key_env):
        assert secrets_store.resolve_secret(db_path, "alice", "monarch", "email") is None


class TestImport:
    def _user_config_with_karakeep(self, **kwargs) -> UserConfig:
        # _allow_obsolete=True: this fixture simulates the load-time
        # migration window where TOML still carries the retired type and
        # import_from_user_configs is meant to absorb its credentials
        # before the row is dropped. base_url/api_key live in extra after
        # the Resources sunset (no longer flat ResourceConfig fields).
        extra = {
            "base_url": kwargs.get("base_url", ""),
            "api_key": kwargs.get("api_key", ""),
        }
        return UserConfig(
            display_name="Alice",
            timezone="UTC",
            resources=[
                ResourceConfig(
                    type="karakeep",
                    name="Karakeep",
                    extra=extra,
                    _allow_obsolete=True,
                )
            ],
        )

    def test_imports_karakeep_credentials(self, db_path, secret_key_env):
        users = {
            "alice": self._user_config_with_karakeep(
                base_url="https://k.example",
                api_key="abcd",
            )
        }
        n = secrets_store.import_from_user_configs(db_path, users)
        assert n == 2
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "base_url") == "https://k.example"
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "abcd"

    def test_idempotent(self, db_path, secret_key_env):
        users = {
            "alice": self._user_config_with_karakeep(
                base_url="https://k.example", api_key="abcd",
            )
        }
        secrets_store.import_from_user_configs(db_path, users)
        # Second run inserts nothing new.
        assert secrets_store.import_from_user_configs(db_path, users) == 0

    def test_does_not_overwrite_existing_secret(self, db_path, secret_key_env):
        """A user may have already set a value via web UI; the import should not
        clobber it with a stale TOML default."""
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "set-via-web")
        users = {
            "alice": self._user_config_with_karakeep(api_key="from-toml")
        }
        secrets_store.import_from_user_configs(db_path, users)
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "set-via-web"

    def test_imports_karakeep_base_url_and_api_key(self, db_path, secret_key_env):
        """Both endpoint and key now flow into the encrypted store — once
        the karakeep resource type is retired by the modules refactor, the
        secrets table is the only place these values live."""
        uc = UserConfig(
            display_name="Alice", timezone="UTC",
            resources=[
                ResourceConfig(
                    type="karakeep",
                    name="Karakeep",
                    extra={"base_url": "https://k.example", "api_key": "abcd"},
                    _allow_obsolete=True,
                ),
            ],
        )
        n = secrets_store.import_from_user_configs(db_path, {"alice": uc})
        assert n == 2
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "abcd"
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "base_url") == "https://k.example"

    def test_imports_overland_ingest_token(self, db_path, secret_key_env):
        """Overland ingest_token migrates from `[[resources]] extras` into
        the secrets table — webhook_receiver scans the table at startup."""
        uc = UserConfig(
            display_name="Alice", timezone="UTC",
            resources=[
                ResourceConfig(
                    type="overland",
                    name="GPS",
                    extra={"ingest_token": "tok-xyz"},
                    _allow_obsolete=True,
                ),
            ],
        )
        n = secrets_store.import_from_user_configs(db_path, {"alice": uc})
        assert n == 1
        assert secrets_store.get_secret(db_path, "alice", "overland", "ingest_token") == "tok-xyz"

    def test_skips_when_key_missing(self, db_path):
        """No-op when ISTOTA_SECRET_KEY isn't set — best-effort, falls back to
        TOML extras at resolution time."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOTA_SECRET_KEY", None)
            users = {"alice": self._user_config_with_karakeep(api_key="x")}
            assert secrets_store.import_from_user_configs(db_path, users) == 0

    def test_does_not_re_encrypt_after_key_rotation(self, db_path):
        """Regression: get_secret returns None both for "missing" and
        "decrypt-failed", which would let an operator key rotation silently
        re-import stale TOML over a web-UI-managed value. The fix is the
        decrypt-free ``secret_exists`` check inside the import."""
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "k1" * 16}):
            secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "set-via-web")
        # Operator rotates the key. Existing rows are now undecryptable.
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "k2" * 16}):
            users = {"alice": self._user_config_with_karakeep(api_key="from-toml")}
            written = secrets_store.import_from_user_configs(db_path, users)
            # Import must not write — the row exists, even if it can't be read.
            assert written == 0


class TestExists:
    def test_returns_true_for_present_row(self, db_path, secret_key_env):
        secrets_store.set_secret(db_path, "alice", "monarch", "email", "x@y")
        assert secrets_store.secret_exists(db_path, "alice", "monarch", "email") is True

    def test_returns_false_for_missing_row(self, db_path, secret_key_env):
        assert secrets_store.secret_exists(db_path, "alice", "monarch", "email") is False

    def test_returns_true_even_when_undecryptable(self, db_path):
        """secret_exists must not depend on the master key — that's the point
        of having it as a separate primitive."""
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "k1" * 16}):
            secrets_store.set_secret(db_path, "alice", "monarch", "email", "x")
        with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "k2" * 16}):
            assert secrets_store.secret_exists(db_path, "alice", "monarch", "email") is True
            assert secrets_store.get_secret(db_path, "alice", "monarch", "email") is None
