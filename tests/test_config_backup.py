"""`config_editor.backup`: gathering saved state, the destination policy, the
shared limits and the conversion back into an editor change set."""

from __future__ import annotations

import asyncio
import json
import os
import tomllib

import pytest

from istota.apply import CheckResult
from istota.config import Config
from istota.config_editor import backup, edits, requests
from istota.config_editor.backup import (
    Bundle, BackupError, FileCredential, InlineCredential,
)
from istota.config_editor.edits import ChangeError
from istota.config_editor.secrets import SENTINEL
from istota.credentials.kdbx_import import VAULT_MAX_ENTRIES
from istota.credentials.names import VAULT_MAX_VALUE_BYTES

IMAP_FILE = "imap-file-value-31"
IMAP_INLINE = "imap-inline-value-32"
SMTP_INLINE = "smtp-inline-value-33"
SESSION_FILE = "session-file-value-34"
SESSION_INLINE = "session-inline-value-35"
OWN_KEY = "own-provider-key-36"
ENV_ONLY = "env-only-value-37"

SECRET_VALUES = (IMAP_FILE, IMAP_INLINE, SMTP_INLINE, SESSION_FILE, SESSION_INLINE, OWN_KEY, ENV_ONLY)


@pytest.fixture
def install(tmp_path):
    config_path = tmp_path / "config.toml"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    return config_path, secrets_dir


def _write_secret(secrets_dir, name, value):
    (secrets_dir / name).write_text(value, encoding="utf-8")


def _gather(install, text, environ=None, **kwargs):
    config_path, secrets_dir = install
    config_path.write_text(text, encoding="utf-8")
    return backup.gather_backup(config_path, secrets_dir, environ=environ or {}, **kwargs)


def _files(bundle):
    return {item.target: (item.source, item.value) for item in bundle.file_credentials}


def _omitted(bundle, category):
    return {item.name for item in bundle.omissions if item.category == category}


def _no_secret_in(text):
    for value in SECRET_VALUES:
        assert value not in text


class TestSourcePrecedence:
    def test_a_file_value_is_gathered_with_its_trailing_newlines_dropped(self, install):
        _write_secret(install[1], "istota_email_imap_password", IMAP_FILE + "\n\n")
        bundle = _gather(install, '[email]\nimap_host = "imap.example.org"\n')
        assert _files(bundle) == {"istota_email_imap_password": ("file", IMAP_FILE)}

    def test_a_mapped_inline_value_with_no_file_is_a_file_credential_from_inline(self, install):
        bundle = _gather(install, f'[email]\nsmtp_password = "{SMTP_INLINE}"\n')
        assert _files(bundle) == {"istota_email_smtp_password": ("inline", SMTP_INLINE)}
        assert bundle.inline_credentials == []
        # The attachment keeps its sentinel at the mapped path.
        assert tomllib.loads(bundle.config_toml) == {"email": {"smtp_password": SENTINEL}}

    def test_an_empty_secret_file_falls_back_to_the_inline_value(self, install):
        _write_secret(install[1], "istota_email_smtp_password", "\n")
        bundle = _gather(install, f'[email]\nsmtp_password = "{SMTP_INLINE}"\n')
        assert _files(bundle) == {"istota_email_smtp_password": ("inline", SMTP_INLINE)}

    def test_a_missing_secret_directory_still_gathers_inline_values(self, install, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(f'[email]\nsmtp_password = "{SMTP_INLINE}"\n', encoding="utf-8")
        bundle = backup.gather_backup(config_path, tmp_path / "absent", environ={})
        assert _files(bundle) == {"istota_email_smtp_password": ("inline", SMTP_INLINE)}
        assert bundle.secrets_dir_available is False
        assert bundle.summary()["secrets_dir_available"] is False

    def test_the_file_wins_over_a_different_inline_value_and_says_so(self, install):
        _write_secret(install[1], "istota_email_imap_password", IMAP_FILE)
        bundle = _gather(install, f'[email]\nimap_password = "{IMAP_INLINE}"\n')
        assert _files(bundle) == {"istota_email_imap_password": ("file", IMAP_FILE)}
        assert _omitted(bundle, "shadowed_inline") == {"email.imap_password"}
        _no_secret_in(json.dumps(bundle.summary()))
        _no_secret_in(repr(bundle))

    def test_a_legacy_spelling_is_read_at_its_mapped_path(self, install):
        bundle = _gather(install, '[whatsapp]\naccess_token = "wa-token-value"\n')
        assert _files(bundle) == {"istota_whatsapp_access_token": ("inline", "wa-token-value")}
        assert bundle.inline_credentials == []

    def test_an_inline_header_table_is_exported_as_its_secret_files_json(self, install):
        bundle = _gather(install, '[brain.native.extra_headers]\nX-Key = "header-value-38"\n')
        name = "istota_brain_native_extra_headers"
        assert _files(bundle) == {name: ("inline", json.dumps({"X-Key": "header-value-38"}))}
        assert bundle.inline_credentials == []
        assert "header-value-38" not in bundle.config_toml

        change_set, warnings = backup.restore_change_set(
            bundle, "", restore_config=True, selected=[f"file:{name}"], base_digest="d" * 64,
        )
        assert change_set["secrets"] == {name: json.dumps({"X-Key": "header-value-38"})}
        assert "header-value-38" not in change_set["raw"]
        assert not any("stays unset" in item["message"] for item in warnings)
        found = _preview("", change_set)
        assert not found.blocked, (found.errors, found.blocking)
        assert found.file_secrets == change_set["secrets"]
        assert found.inline_secrets == {}

    def test_a_header_table_with_a_non_string_value_refuses(self, install):
        with pytest.raises(BackupError) as caught:
            _gather(install, '[brain.native.extra_headers]\nX-Key = "header-value-38"\nX-Count = 3\n')
        assert caught.value.code == "invalid_saved_credential"
        assert "header-value-38" not in str(caught.value)

    def test_an_environment_only_credential_is_omitted_not_exported(self, install):
        bundle = _gather(install, "", environ={
            "ISTOTA_CALDAV_PASSWORD": ENV_ONLY,
            "ISTOTA_SMS_TWILIO_ACCOUNT_SID": ENV_ONLY,
        })
        assert bundle.credentials == []
        assert {"istota_caldav_password", "sms.twilio.account_sid"} <= _omitted(bundle, "environment")
        _no_secret_in(json.dumps(bundle.summary()))


class TestInlineCredentials:
    def test_unmapped_secret_leaves_keep_their_type(self, install):
        bundle = _gather(install, (
            '[security]\ncredential_exports_per_day = 7\n\n'
            f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\n'
        ))
        values = {item.path: item.value for item in bundle.inline_credentials}
        assert values == {
            ("security", "credential_exports_per_day"): 7,
            ("brain", "native", "providers", "own", "api_key"): OWN_KEY,
        }
        assert isinstance(values[("security", "credential_exports_per_day")], int)
        _no_secret_in(repr(bundle.inline_credentials))

    def test_an_empty_inline_value_is_no_entry(self, install):
        bundle = _gather(install, '[sms.twilio]\naccount_sid = ""\n')
        assert bundle.inline_credentials == []


class TestRuntimeKeys:
    def test_the_session_key_is_gathered_from_neither_source(self, install):
        _write_secret(install[1], "istota_web_session_secret_key", SESSION_FILE)
        bundle = _gather(install, f'[web]\nsession_secret_key = "{SESSION_INLINE}"\n')
        assert bundle.credentials == []
        assert {"istota_web_session_secret_key", "ISTOTA_SECRET_KEY", "ISTOTA_WEB_TOKEN_KEY"} <= _omitted(
            bundle, "runtime_key",
        )
        _no_secret_in(bundle.config_toml + repr(bundle) + json.dumps(bundle.summary()))

    def test_the_generated_browser_token_is_not_gathered(self, install):
        _write_secret(install[1], "istota_browser_api_token", "browser-token-value")
        bundle = _gather(install, "")
        assert "istota_browser_api_token" not in _files(bundle)

    def test_the_policy_refuses_runtime_and_unknown_destinations(self):
        assert backup.destination_refusal("file", "istota_web_session_secret_key")
        assert backup.destination_refusal("file", "istota_browser_api_token")
        assert backup.destination_refusal("file", "../config.toml")
        assert backup.destination_refusal("inline", ("web", "session_secret_key"))
        assert backup.destination_refusal("inline", ("email", "smtp_password"))
        assert backup.destination_refusal("inline", ("email", "imap_host"))
        assert backup.destination_refusal("inline", ("x",) * 17)
        assert backup.destination_refusal("file", "istota_email_smtp_password") is None
        assert backup.destination_refusal("inline", ("brain", "native", "providers", "own", "api_key")) is None


class TestReads:
    def test_a_symlinked_secret_file_refuses(self, install, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text(IMAP_FILE, encoding="utf-8")
        os.symlink(target, install[1] / "istota_email_imap_password")
        with pytest.raises(BackupError) as caught:
            _gather(install, "")
        assert caught.value.code == "unreadable_secret"

    def test_an_oversized_secret_file_refuses(self, install):
        _write_secret(install[1], "istota_email_imap_password", "x" * (VAULT_MAX_VALUE_BYTES + 1))
        with pytest.raises(BackupError) as caught:
            _gather(install, "")
        assert caught.value.code == "too_large"

    @pytest.mark.requires_dac
    def test_an_unreadable_secret_file_refuses_rather_than_falling_back(self, install):
        path = install[1] / "istota_email_smtp_password"
        _write_secret(install[1], "istota_email_smtp_password", "file-value")
        path.chmod(0)
        try:
            with pytest.raises(BackupError) as caught:
                _gather(install, f'[email]\nsmtp_password = "{SMTP_INLINE}"\n')
        finally:
            path.chmod(0o600)
        assert caught.value.code == "unreadable_secret"
        assert SMTP_INLINE not in str(caught.value)

    def test_a_config_that_changes_on_every_read_refuses(self, install, monkeypatch):
        config_path, _ = install
        config_path.write_text("a = 0\n", encoding="utf-8")
        real = backup._read_state
        reads = []

        def changing(*args):
            state = real(*args)
            reads.append(1)
            config_path.write_text(f"a = {len(reads)}\n", encoding="utf-8")
            return state

        monkeypatch.setattr(backup, "_read_state", changing)
        with pytest.raises(BackupError) as caught:
            backup.gather_backup(config_path, install[1], environ={})
        assert caught.value.code == "state_changed"
        assert len(reads) == 4

    def test_one_change_is_retried_once(self, install, monkeypatch):
        config_path, _ = install
        config_path.write_text("a = 0\n", encoding="utf-8")
        real = backup._read_state
        reads = []

        def changes_once(*args):
            state = real(*args)
            reads.append(1)
            if len(reads) == 1:
                config_path.write_text("a = 1\n", encoding="utf-8")
            return state

        monkeypatch.setattr(backup, "_read_state", changes_once)
        bundle = backup.gather_backup(config_path, install[1], environ={})
        assert tomllib.loads(bundle.config_toml) == {"a": 1}

    def test_an_open_change_request_refuses(self, install, db_path):
        from istota import db

        with db.get_db(db_path) as conn:
            requests.create(conn, edits.Preview(document={}), admin_id="alice", base_digest="a" * 64)
            conn.commit()
        with pytest.raises(BackupError) as caught:
            _gather(install, "", db_path=db_path)
        assert caught.value.code == "request_open"


def _bundle(config_toml="", file_credentials=(), inline_credentials=()):
    return Bundle(
        config_toml=config_toml,
        file_credentials=list(file_credentials),
        inline_credentials=list(inline_credentials),
    )


def _toml_of_bytes(size: int, filler: str) -> str:
    """A config whose preamble plus one string line is exactly `size` UTF-8
    bytes, the string mostly `filler` as TOML escapes it."""
    escaped = "\\" + filler if filler in ('"', "\\") else filler
    head = backup.CONFIG_PREAMBLE + 'k = "'
    tail = '"\n'
    room = size - len((head + tail).encode("utf-8"))
    count, pad = divmod(room, len(escaped.encode("utf-8")))
    return head + escaped * count + "a" * pad + tail


class TestLimits:
    @pytest.mark.parametrize("filler", ["a", "\u00e9", "\u4e2d", '"', "\\"])
    def test_the_configuration_limit_counts_utf8_bytes_including_the_preamble(self, filler):
        exact = _toml_of_bytes(backup.CONFIG_MAX_BYTES, filler)
        assert len(exact.encode("utf-8")) == backup.CONFIG_MAX_BYTES
        assert len(exact) < backup.CONFIG_MAX_BYTES or filler.isascii()
        backup.validate_bundle(_bundle(exact))
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(_toml_of_bytes(backup.CONFIG_MAX_BYTES + 1, filler)))
        assert caught.value.code == "too_large"
        assert str(backup.CONFIG_MAX_BYTES) in str(caught.value)

    def test_a_300_kib_toml_string_refuses_gather(self, install):
        with pytest.raises(BackupError) as caught:
            _gather(install, f'note = "{"x" * (300 * 1024)}"\n')
        assert caught.value.code == "too_large"

    def test_one_value_at_the_limit_passes_and_one_byte_over_refuses(self):
        at = FileCredential("istota_email_imap_password", "file", "é" * (VAULT_MAX_VALUE_BYTES // 2))
        backup.validate_bundle(_bundle(file_credentials=[at]))
        over = FileCredential("istota_email_imap_password", "file", at.value + "x")
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(file_credentials=[over]))
        assert caught.value.code == "too_large"
        assert at.value not in str(caught.value)

    def test_an_inline_value_is_measured_as_json(self):
        path = ("brain", "native", "providers", "own", "api_key")
        # Each quote is two bytes in JSON, plus the two enclosing quotes.
        at = InlineCredential(path, '"' * ((VAULT_MAX_VALUE_BYTES - 2) // 2))
        backup.validate_bundle(_bundle(inline_credentials=[at]))
        with pytest.raises(BackupError):
            backup.validate_bundle(_bundle(inline_credentials=[InlineCredential(path, at.value + '"')]))

    def test_the_aggregate_limit_is_the_canonical_records(self):
        names = [name for name in backup._gathered_names()]
        credentials = [FileCredential(name, "file", "v" * 8000) for name in names[:20]]
        assert len(backup.canonical_credentials(credentials)) < backup.CREDENTIALS_MAX_BYTES
        many = [
            InlineCredential(("brain", "native", "providers", f"p{index}", "api_key"), "v" * 8000)
            for index in range(40)
        ]
        size = len(backup.canonical_credentials(many))
        assert size > backup.CREDENTIALS_MAX_BYTES
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(inline_credentials=many))
        assert caught.value.code == "too_large"
        assert "credentials" in str(caught.value)

    def test_the_aggregate_limit_is_inclusive_to_the_byte(self):
        def records(last: int):
            values = ["v" * 7900] * 32 + ["v" * last]
            return [
                InlineCredential(("brain", "native", "providers", f"p{index:02}", "api_key"), value)
                for index, value in enumerate(values)
            ]

        short = len(backup.canonical_credentials(records(0)))
        last = backup.CREDENTIALS_MAX_BYTES - short
        assert 0 < last < VAULT_MAX_VALUE_BYTES
        exact = records(last)
        assert len(backup.canonical_credentials(exact)) == backup.CREDENTIALS_MAX_BYTES
        backup.validate_bundle(_bundle(inline_credentials=exact))
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(inline_credentials=records(last + 1)))
        assert caught.value.code == "too_large"

    def test_the_canonical_encoding_is_order_independent(self):
        a = FileCredential("istota_email_imap_password", "file", "x")
        b = InlineCredential(("brain", "native", "providers", "own", "api_key"), 3)
        assert backup.canonical_credentials([a, b]) == backup.canonical_credentials([b, a])

    def test_the_entry_count_includes_manifest_and_configuration(self):
        def entries(count):
            return [
                InlineCredential(("brain", "native", "providers", f"p{index}", "api_key"), "v")
                for index in range(count)
            ]

        backup.validate_bundle(_bundle(inline_credentials=entries(VAULT_MAX_ENTRIES - 2)))
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(inline_credentials=entries(VAULT_MAX_ENTRIES - 1)))
        assert caught.value.code == "too_large"

    def test_duplicate_and_forbidden_destinations_are_invalid(self):
        same = FileCredential("istota_email_imap_password", "file", "x")
        with pytest.raises(BackupError) as caught:
            backup.validate_bundle(_bundle(file_credentials=[same, same]))
        assert caught.value.code == "invalid_backup"
        for bad in (
            _bundle(file_credentials=[FileCredential("istota_web_session_secret_key", "file", "x")]),
            _bundle(file_credentials=[FileCredential("istota_email_imap_password", "elsewhere", "x")]),
            _bundle(inline_credentials=[InlineCredential(("email", "smtp_password"), "x")]),
            _bundle(inline_credentials=[InlineCredential(("brain", "native", "providers", "o", "api_key"), [1])]),
        ):
            with pytest.raises(BackupError) as caught:
                backup.validate_bundle(bad)
            assert caught.value.code == "invalid_backup"


def _preview(current_text, change_set, environ=None):
    async def run_check(document, **kwargs):
        return CheckResult()

    return asyncio.run(edits.preview(
        current_text, change_set, config=Config(admin_users={"alice"}), acting_admin="alice",
        run_check=run_check, environ=environ or {},
    ))


class TestRestoreChangeSet:
    def test_an_inline_origin_file_credential_restores_to_its_secret_file(self, install):
        bundle = _gather(install, f'[email]\nimap_host = "old"\nsmtp_password = "{SMTP_INLINE}"\n')
        current = '[email]\nimap_host = "new"\n'
        change_set, warnings = backup.restore_change_set(
            bundle, current, restore_config=True,
            selected=["file:istota_email_smtp_password"], base_digest="d" * 64,
        )
        assert change_set["secrets"] == {"istota_email_smtp_password": SMTP_INLINE}
        assert SMTP_INLINE not in change_set["raw"]
        assert any("secret file istota_email_smtp_password" in item["message"] for item in warnings)
        assert not any("stays unset" in item["message"] for item in warnings)
        found = _preview(current, change_set)
        assert not found.blocked, (found.errors, found.blocking)
        assert found.file_secrets == {"istota_email_smtp_password": SMTP_INLINE}
        assert found.document == {"email": {"imap_host": "old"}}
        _no_secret_in(json.dumps(found.as_json()))

    def test_a_selected_inline_credential_lands_where_the_target_has_none(self, install):
        path = ("brain", "native", "providers", "own", "api_key")
        bundle = _gather(install, f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\nbase_url = "https://p.example"\n')
        current = ""
        change_set, warnings = backup.restore_change_set(
            bundle, current, restore_config=True, selected=["inline:brain.native.providers.own.api_key"],
            base_digest="d" * 64,
        )
        assert warnings == []
        found = _preview(current, change_set)
        assert found.inline_secrets == {"brain.native.providers.own.api_key": OWN_KEY}
        assert edits.secrets.lookup(found.document, path) == (True, SENTINEL)

    def test_an_unselected_missing_inline_credential_is_dropped_and_named(self, install):
        bundle = _gather(install, f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\nbase_url = "https://p.example"\n')
        change_set, warnings = backup.restore_change_set(
            bundle, "", restore_config=True, selected=[], base_digest="d" * 64,
        )
        assert [item["path"] for item in warnings] == [["brain", "native", "providers", "own", "api_key"]]
        found = _preview("", change_set)
        assert found.inline_secrets == {}
        assert OWN_KEY not in change_set["raw"]

    def test_a_current_value_is_kept_where_the_sentinel_has_one(self, install):
        bundle = _gather(install, f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\n')
        current = '[brain.native.providers.own]\napi_key = "current-key-39"\n'
        change_set, warnings = backup.restore_change_set(
            bundle, current, restore_config=True, selected=[], base_digest="d" * 64,
        )
        assert warnings == []
        assert not _preview(current, change_set).changed

    def test_credentials_only_keeps_the_current_document(self, install):
        _write_secret(install[1], "istota_email_imap_password", IMAP_FILE)
        bundle = _gather(install, '[email]\nimap_host = "old"\n')
        current = '[email]\nimap_host = "new"\n'
        change_set, _ = backup.restore_change_set(
            bundle, current, restore_config=False,
            selected=["file:istota_email_imap_password"], base_digest="d" * 64,
        )
        found = _preview(current, change_set)
        assert found.diff == []
        assert found.file_secrets == {"istota_email_imap_password": IMAP_FILE}

    def test_an_unchanged_file_credential_is_left_out(self, install):
        _write_secret(install[1], "istota_email_imap_password", IMAP_FILE)
        bundle = _gather(install, "")
        change_set, _ = backup.restore_change_set(
            bundle, "", restore_config=False, selected=["file:istota_email_imap_password"],
            base_digest="d" * 64, current_files={"istota_email_imap_password": IMAP_FILE},
        )
        assert change_set["secrets"] == {}
        assert not _preview("", change_set).changed

    def test_a_selection_outside_the_backup_refuses(self, install):
        bundle = _gather(install, "")
        with pytest.raises(ChangeError):
            backup.restore_change_set(
                bundle, "", restore_config=True, selected=["file:istota_caldav_password"],
                base_digest="d" * 64,
            )
