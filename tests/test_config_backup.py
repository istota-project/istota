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


# --- the encrypted backup -----------------------------------------------------

BACKUP_PASSWORD = "SENTINEL-backup-password-4c1d"
TYPED_SECRET = 'typed "secret" \\ 39'
USER_VAULT_VALUE = "user-vault-value-40"


@pytest.fixture
def cheap(monkeypatch):
    """pykeepass's database at a test-sized Argon2, through the same code."""
    from istota.credentials import kdbx_export
    from tests.support.kdbx import _template_bytes, create_database

    _template_bytes()
    monkeypatch.setattr("pykeepass.create_database", create_database)
    return kdbx_export.ExportOptions(64, 1, 1)


def _full_bundle(install):
    _write_secret(install[1], "istota_email_imap_password", IMAP_FILE)
    return _gather(install, (
        f'[email]\nimap_host = "imap.example.org"\nsmtp_password = "{SMTP_INLINE}"\n\n'
        '[security]\ncredential_exports_per_day = 7\n\n'
        f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\n'
    ))


def _export(bundle, cheap, keyfile=None):
    return backup.build_installation_kdbx(bundle, password=BACKUP_PASSWORD, keyfile=keyfile, options=cheap)


async def _open(data, db_path, password=BACKUP_PASSWORD, keyfile=None):
    return await backup.open_installation_backup(data, password=password, keyfile=keyfile, db_path=db_path)


def _keepass(data, keyfile=None):
    import io

    from pykeepass import PyKeePass

    return PyKeePass(io.BytesIO(data), password=BACKUP_PASSWORD,
                     keyfile=io.BytesIO(keyfile) if keyfile is not None else None)


def _keyfile():
    import hashlib

    data = bytes(range(32))
    digest = hashlib.sha256(data).hexdigest()[:8].upper()
    return (
        f'<KeyFile><Meta><Version>2.0</Version></Meta><Key><Data Hash="{digest}">'
        f'{data.hex()}</Data></Key></KeyFile>'
    ).encode()


class TestTheEncryptedLayout:
    def test_keepass_reads_the_layout_without_istota(self, install, cheap):
        import hashlib

        bundle = _full_bundle(install)
        kp = _keepass(_export(bundle, cheap))
        assert [group.name for group in kp.root_group.subgroups] == ["Istota installation backup"]
        root = kp.root_group.subgroups[0]
        assert sorted(group.name for group in root.subgroups) == ["Deployment credentials", "Inline credentials"]
        manifest = next(entry for entry in root.entries if entry.title == "Backup manifest")
        props = manifest.custom_properties
        assert props["istota.format"] == "installation-backup"
        assert props["istota.version"] == "1"
        assert props["istota.recovery_scope"] == "configuration-and-installation-credentials"
        assert "the original credential-store master key" in json.loads(props["istota.recovery_requires"])
        omitted = {item["name"] for item in json.loads(props["istota.omissions"])}
        assert {"istota_web_session_secret_key", "ISTOTA_SECRET_KEY", "ISTOTA_WEB_TOKEN_KEY"} <= omitted
        configuration = next(entry for entry in root.entries if entry.title == "Configuration")
        [attachment] = configuration.attachments
        assert attachment.filename == "config.toml"
        assert attachment.data.decode() == bundle.config_toml
        assert props["istota.config_sha256"] == hashlib.sha256(attachment.data).hexdigest()
        _no_secret_in(attachment.data.decode())
        _no_secret_in(json.dumps(props))

        deployment = next(group for group in root.subgroups if group.name == "Deployment credentials")
        files = {entry.custom_properties["istota.target"]: entry for entry in deployment.entries}
        assert files["istota_email_imap_password"].password == IMAP_FILE
        assert files["istota_email_imap_password"].custom_properties["istota.source"] == "file"
        assert files["istota_email_smtp_password"].password == SMTP_INLINE
        assert files["istota_email_smtp_password"].custom_properties["istota.source"] == "inline"
        assert files["istota_email_smtp_password"].title == "email.smtp_password (istota_email_smtp_password)"
        inline = next(group for group in root.subgroups if group.name == "Inline credentials")
        typed = {entry.title: entry for entry in inline.entries}
        counter = typed["security.credential_exports_per_day"]
        assert counter.password == "7"
        assert counter.custom_properties["istota.value"] == "7"
        assert counter.is_custom_property_protected("istota.value")
        assert json.loads(counter.custom_properties["istota.path"]) == ["security", "credential_exports_per_day"]
        assert all(not entry.history for entry in kp.entries)
        assert len(kp.attachments) == 1

    def test_a_key_file_is_required_when_given(self, install, cheap):
        from pykeepass.exceptions import CredentialsError

        data = _export(_full_bundle(install), cheap, keyfile=_keyfile())
        with pytest.raises(CredentialsError):
            _keepass(data)
        _keepass(data, keyfile=_keyfile())

    def test_an_invalid_key_file_refuses_before_any_work(self, install, cheap):
        with pytest.raises(BackupError) as caught:
            _export(_full_bundle(install), cheap, keyfile=b"not a key file")
        assert caught.value.code == "keyfile_invalid"

    def test_the_finished_file_is_held_to_the_import_cap(self, install, cheap, monkeypatch):
        from istota.credentials import kdbx_import

        bundle = _full_bundle(install)
        size = len(_export(bundle, cheap))
        monkeypatch.setattr(kdbx_import, "VAULT_READ_CAP_BYTES", size - 1)
        with pytest.raises(BackupError) as caught:
            _export(bundle, cheap)
        assert caught.value.code == "too_large"
        assert str(size - 1) in str(caught.value)

    def test_an_oversized_bundle_refuses_before_the_key_derivation(self, cheap, monkeypatch):
        from istota.credentials import kdbx_export

        def never(**kwargs):
            raise AssertionError("derived a key for a bundle over its limit")

        monkeypatch.setattr(kdbx_export, "new_database", never)
        with pytest.raises(BackupError) as caught:
            _export(_bundle(_toml_of_bytes(backup.CONFIG_MAX_BYTES + 1, "a")), cheap)
        assert caught.value.code == "too_large"


def _at_the_limits():
    """A bundle with the configuration and the credential aggregate each at
    exactly its limit."""
    def records(last: int):
        values = ["v" * 7900] * 32 + ["v" * last]
        return [
            InlineCredential(("brain", "native", "providers", f"p{index:02}", "api_key"), value)
            for index, value in enumerate(values)
        ]

    last = backup.CREDENTIALS_MAX_BYTES - len(backup.canonical_credentials(records(0)))
    credentials = records(last)
    assert len(backup.canonical_credentials(credentials)) == backup.CREDENTIALS_MAX_BYTES
    config = _toml_of_bytes(backup.CONFIG_MAX_BYTES, "中")
    assert len(config.encode()) == backup.CONFIG_MAX_BYTES
    return _bundle(config, inline_credentials=credentials)


class TestRoundTrip:
    async def test_an_export_opens_and_parses_to_the_same_bundle(self, install, cheap, tmp_path):
        bundle = _full_bundle(install)
        opened = await _open(_export(bundle, cheap), tmp_path / "db.sqlite")
        assert opened.config_toml == bundle.config_toml
        assert _files(opened) == _files(bundle)
        assert {(item.path, item.value) for item in opened.inline_credentials} == {
            (item.path, item.value) for item in bundle.inline_credentials
        }
        counter = next(item for item in opened.inline_credentials if item.path[-1] == "credential_exports_per_day")
        assert counter.value == 7 and type(counter.value) is int
        assert opened.omissions == bundle.omissions
        assert opened.manifest.recovery_scope == backup.RECOVERY_SCOPE
        assert opened.secrets_dir_available is True
        _no_secret_in(repr(opened))
        _no_secret_in(json.dumps(opened.summary()))

    async def test_an_archive_at_every_limit_opens_through_the_import_validator(self, cheap, tmp_path):
        bundle = _at_the_limits()
        opened = await _open(_export(bundle, cheap), tmp_path / "db.sqlite")
        assert opened.config_toml == bundle.config_toml
        assert opened.inline_credentials == bundle.inline_credentials

    async def test_a_typed_string_with_quotes_and_backslashes_round_trips(self, cheap, tmp_path):
        bundle = _bundle(backup.CONFIG_PREAMBLE, inline_credentials=[
            InlineCredential(("brain", "native", "providers", "own", "api_key"), TYPED_SECRET),
            InlineCredential(("security", "credential_exports_per_day"), True),
        ])
        opened = await _open(_export(bundle, cheap), tmp_path / "db.sqlite")
        assert opened.inline_credentials == bundle.inline_credentials
        assert type(opened.inline_credentials[1].value) is bool

    async def test_a_key_file_backup_opens_only_with_it(self, install, cheap, tmp_path):
        data = _export(_full_bundle(install), cheap, keyfile=_keyfile())
        with pytest.raises(BackupError) as caught:
            await _open(data, tmp_path / "db.sqlite")
        assert caught.value.code == "backup_locked"
        opened = await _open(data, tmp_path / "db.sqlite", keyfile=_keyfile())
        assert _files(opened)

    async def test_a_wrong_password_is_backup_locked_and_says_nothing(self, install, cheap, tmp_path):
        with pytest.raises(BackupError) as caught:
            await _open(_export(_full_bundle(install), cheap), tmp_path / "db.sqlite", password="wrong")
        assert caught.value.code == "backup_locked"
        _no_secret_in(str(caught.value))

    async def test_bytes_that_are_not_keepass_are_invalid(self, tmp_path):
        with pytest.raises(BackupError) as caught:
            await _open(b"not a keepass file at all", tmp_path / "db.sqlite")
        assert caught.value.code == "invalid_backup"

    async def test_a_restored_inline_origin_credential_goes_to_its_secret_file(self, install, cheap, tmp_path):
        bundle = _gather(install, f'[email]\nimap_host = "old"\nsmtp_password = "{SMTP_INLINE}"\n')
        opened = await _open(_export(bundle, cheap), tmp_path / "db.sqlite")
        change_set, _ = backup.restore_change_set(
            opened, '[email]\nimap_host = "new"\n', restore_config=True,
            selected=["file:istota_email_smtp_password"], base_digest="d",
        )
        assert change_set["secrets"] == {"istota_email_smtp_password": SMTP_INLINE}
        assert SMTP_INLINE not in change_set["raw"]


class TestTheAdminRestoreRefuses:
    async def test_an_ordinary_user_vault(self, cheap, tmp_path):
        from tests.support.kdbx import create_database

        path = tmp_path / "user.kdbx"
        kp = create_database(str(path), password=BACKUP_PASSWORD)
        group = kp.add_group(kp.root_group, "istota")
        kp.add_entry(group, "aws", "", USER_VAULT_VALUE)
        kp.save()
        with pytest.raises(BackupError) as caught:
            await _open(path.read_bytes(), tmp_path / "db.sqlite")
        assert caught.value.code == "invalid_backup"
        assert USER_VAULT_VALUE not in str(caught.value)

    async def test_an_attachment_over_the_configuration_limit(self, install, cheap, tmp_path, monkeypatch):
        bundle = _full_bundle(install)
        data = _export(bundle, cheap)
        monkeypatch.setattr(backup, "CONFIG_MAX_BYTES", len(bundle.config_toml.encode()) - 1)
        with pytest.raises(BackupError) as caught:
            await _open(data, tmp_path / "db.sqlite")
        assert caught.value.code == "too_large"

    async def test_a_file_over_the_import_cap_before_any_decrypt(self, tmp_path, monkeypatch):
        from istota.credentials import kdbx_import, kdbx_worker

        def never(*args, **kwargs):
            raise AssertionError("spawned a decrypt for a file over the cap")

        monkeypatch.setattr(kdbx_worker, "open_group", never)
        with pytest.raises(BackupError) as caught:
            await _open(b"x" * (kdbx_import.VAULT_READ_CAP_BYTES + 1), tmp_path / "db.sqlite")
        assert caught.value.code == "too_large"


def _worker_answer(config=backup.CONFIG_PREAMBLE):
    """A worker answer in the exported layout, to edit before parsing."""
    import base64
    import hashlib

    manifest = _answer_entry("Backup manifest", {
        "istota.format": "installation-backup", "istota.version": "1", "istota.entry": "manifest",
        "istota.config_sha256": hashlib.sha256(config.encode()).hexdigest(),
        "istota.omissions": "[]", "istota.recovery_requires": "[]",
    })
    configuration = _answer_entry("Configuration", {"istota.entry": "configuration"}, attachments=[
        {"filename": "config.toml", "data": base64.b64encode(config.encode()).decode()},
    ])
    return {"matches": 1, "group": {
        "name": "Istota installation backup", "entries": [manifest, configuration], "groups": [],
    }}


def _answer_entry(title, properties, password="", attachments=()):
    return {"title": title, "username": "", "password": password, "url": "", "notes": "",
            "properties": properties, "protected": [], "attachments": list(attachments), "history": 0}


def _file_entry(target, value="v"):
    return _answer_entry(target, {"istota.kind": "file", "istota.target": target, "istota.source": "file"}, value)


def _inline_entry(path, value):
    return _answer_entry("x", {"istota.kind": "inline", "istota.path": path, "istota.value": value})


class TestTheStrictParser:
    def test_the_minimal_layout_parses(self):
        bundle = backup.parse_installation_backup(_worker_answer())
        assert bundle.config_toml == backup.CONFIG_PREAMBLE and bundle.credentials == []

    @pytest.mark.parametrize("change", [
        "no_group", "two_groups", "version", "digest", "no_manifest", "two_configurations",
        "extra_entry", "extra_attachment", "attachment_name", "unknown_group", "nested_group",
        "forbidden_target", "unknown_target", "duplicate_target", "empty_value", "mapped_inline_path",
        "nan_value", "table_value", "bad_toml", "manifest_attachment",
    ])
    def test_each_departure_refuses_the_whole_file(self, change):
        opened = _worker_answer("a = \n" if change == "bad_toml" else "a = 1\n")
        group = opened["group"]
        manifest, configuration = group["entries"]
        deployment = {"name": "Deployment credentials", "entries": [], "groups": []}
        inline = {"name": "Inline credentials", "entries": [], "groups": []}
        group["groups"] = [deployment, inline]
        provider = '["brain","native","providers","o","api_key"]'

        if change == "no_group":
            opened = {"matches": 0, "group": None}
        elif change == "two_groups":
            opened["matches"] = 2
        elif change == "version":
            manifest["properties"]["istota.version"] = "2"
        elif change == "digest":
            manifest["properties"]["istota.config_sha256"] = "0" * 64
        elif change == "no_manifest":
            group["entries"] = [configuration]
        elif change == "two_configurations":
            group["entries"].append(configuration)
        elif change == "extra_entry":
            group["entries"].append(_answer_entry("notes", {}))
        elif change == "extra_attachment":
            deployment["entries"].append({**_file_entry("istota_email_imap_password"),
                                          "attachments": [{"filename": "x", "data": ""}]})
        elif change == "manifest_attachment":
            manifest["attachments"] = [{"filename": "x", "data": ""}]
        elif change == "attachment_name":
            configuration["attachments"][0]["filename"] = "other.toml"
        elif change == "unknown_group":
            group["groups"].append({"name": "Elsewhere", "entries": [], "groups": []})
        elif change == "nested_group":
            deployment["groups"].append({"name": "x", "entries": [], "groups": []})
        elif change == "forbidden_target":
            deployment["entries"].append(_file_entry("istota_web_session_secret_key"))
        elif change == "unknown_target":
            deployment["entries"].append(_file_entry("../etc/passwd"))
        elif change == "duplicate_target":
            deployment["entries"] += [_file_entry("istota_email_imap_password")] * 2
        elif change == "empty_value":
            deployment["entries"].append(_file_entry("istota_email_imap_password", ""))
        elif change == "mapped_inline_path":
            inline["entries"].append(_inline_entry('["email","smtp_password"]', '"v"'))
        elif change == "nan_value":
            inline["entries"].append(_inline_entry(provider, "NaN"))
        elif change == "table_value":
            inline["entries"].append(_inline_entry(provider, '{"a": "b"}'))
        with pytest.raises(BackupError) as caught:
            backup.parse_installation_backup(opened)
        assert caught.value.code == "invalid_backup"

    def test_entry_history_and_foreign_properties_are_ignored(self):
        opened = _worker_answer()
        opened["group"]["entries"][0]["history"] = 3
        opened["group"]["entries"][1]["properties"]["KPXC_DECRYPTION_TIME_PREFERENCE"] = "x"
        backup.parse_installation_backup(opened)


class TestPlainUpload:
    def test_a_redacted_backup_is_a_bundle_with_no_credentials(self, install):
        gathered = _gather(install, f'[email]\nimap_host = "h"\nsmtp_password = "{SMTP_INLINE}"\n')
        bundle = backup.read_plain_backup(gathered.config_toml.encode("utf-8"))
        assert bundle.config_toml == gathered.config_toml
        assert bundle.credentials == []

    @pytest.mark.parametrize("data", [b"", b"  \n\t\n", b"\xff\xfe not utf-8", b"[email\n"])
    def test_empty_malformed_or_unparsable_is_invalid(self, data):
        with pytest.raises(BackupError) as caught:
            backup.read_plain_backup(data)
        assert caught.value.code == "invalid_backup"

    def test_an_encrypted_file_is_not_read_as_toml(self):
        with pytest.raises(BackupError) as caught:
            backup.read_plain_backup(backup.KDBX_SIGNATURE + b"rest")
        assert caught.value.code == "invalid_backup"

    @pytest.mark.parametrize("text", [
        f'[email]\nsmtp_password = "{SMTP_INLINE}"\n',
        f'[brain.native.providers.own]\napi_key = "{OWN_KEY}"\n',
        f'[brain.native.extra_headers]\nX-A = "{IMAP_INLINE}"\n',
    ])
    def test_a_real_credential_value_refuses_without_naming_it(self, text):
        with pytest.raises(BackupError) as caught:
            backup.read_plain_backup(text.encode("utf-8"))
        assert caught.value.code == "plaintext_secrets"
        _no_secret_in(str(caught.value))

    def test_an_empty_credential_is_not_a_value(self):
        backup.read_plain_backup(b'[email]\nsmtp_password = ""\n')

    def test_the_configuration_limit_holds(self):
        filler = "x" * (backup.CONFIG_MAX_BYTES - len('[email]\nimap_host = ""\n'))
        backup.read_plain_backup(f'[email]\nimap_host = "{filler}"\n'.encode())
        with pytest.raises(BackupError) as caught:
            backup.read_plain_backup(f'[email]\nimap_host = "{filler}x"\n'.encode())
        assert caught.value.code == "too_large"


class TestCredentialStatuses:
    def _statuses(self, install, current, files=None):
        bundle = _full_bundle(install)
        return {
            row["id"]: row
            for row in backup.credential_statuses(bundle, current, files or {}, restore_config=True)
        }

    def test_new_changed_unchanged_and_host_only(self, install):
        _write_secret(install[1], "istota_nextcloud_app_password", "nc-app-password-41")
        rows = self._statuses(install, (
            f'[email]\nsmtp_password = "{SMTP_INLINE}"\n\n'
            '[brain.native.providers.own]\napi_key = "another-key"\n'
        ), {"istota_email_imap_password": "rotated-42"})
        assert {key: row["status"] for key, row in rows.items()} == {
            "file:istota_email_imap_password": "changed",
            # Unchanged through the mapped inline fallback.
            "file:istota_email_smtp_password": "unchanged",
            "file:istota_nextcloud_app_password": "host_only",
            "inline:brain.native.providers.own.api_key": "changed",
            "inline:security.credential_exports_per_day": "new",
        }
        assert rows["file:istota_nextcloud_app_password"]["reason"]
        assert [key for key, row in rows.items() if row["selectable"]] == [
            "file:istota_email_imap_password",
            "inline:brain.native.providers.own.api_key",
            "inline:security.credential_exports_per_day",
        ]
        assert rows["file:istota_email_smtp_password"]["source"] == "inline"
        assert rows["inline:security.credential_exports_per_day"]["source"] == "inline"
        _no_secret_in(json.dumps(rows))
        assert "nc-app-password-41" not in json.dumps(rows)

    def test_a_typed_value_compares_by_type(self, install):
        rows = self._statuses(install, '[security]\ncredential_exports_per_day = 7.0\n')
        assert rows["inline:security.credential_exports_per_day"]["status"] == "changed"
        rows = self._statuses(install, '[security]\ncredential_exports_per_day = 7\n')
        assert rows["inline:security.credential_exports_per_day"]["status"] == "unchanged"

    def test_the_current_files_are_read_as_a_gather_reads_them(self, install, tmp_path):
        _write_secret(install[1], "istota_email_imap_password", IMAP_FILE + "\n")
        _write_secret(install[1], "istota_email_smtp_password", "\n")
        assert backup.current_secret_files(install[1]) == {"istota_email_imap_password": IMAP_FILE}
        assert backup.current_secret_files(tmp_path / "absent") == {}
        (install[1] / "istota_email_smtp_password").unlink()
        (install[1] / "istota_email_smtp_password").symlink_to(install[1] / "istota_email_imap_password")
        with pytest.raises(BackupError) as caught:
            backup.current_secret_files(install[1])
        assert caught.value.code == "unreadable_secret"


RESOURCE = "inline:users.alice.resources.0.password"
RESOURCE_PATH = ("users", "alice", "resources", "0", "password")
BACKED_UP_KEY = "resource-key-a-43"
CURRENT_KEY = "resource-key-b-44"


def _resource(name, password):
    return f'[[users.alice.resources]]\ntype = "caldav"\nname = "{name}"\npassword = "{password}"\n'


class TestArrayCredentials:
    """An inline credential inside an array is recorded by index, so it may
    only be put back where the element at that index is the backed-up one
    (`secrets.same_elements`), or where the backup's document is restored."""

    def _row(self, bundle, current, *, restore_config):
        rows = backup.credential_statuses(bundle, current, {}, restore_config=restore_config)
        return {row["id"]: row for row in rows}[RESOURCE]

    def test_a_different_element_is_not_selectable_for_a_credentials_only_restore(self, install):
        bundle = _gather(install, _resource("a", BACKED_UP_KEY))
        current = _resource("b", CURRENT_KEY)
        row = self._row(bundle, current, restore_config=False)
        assert row["status"] == "host_only" and not row["selectable"]
        assert row["reason"]
        assert BACKED_UP_KEY not in json.dumps(row) and CURRENT_KEY not in json.dumps(row)
        with pytest.raises(ChangeError):
            backup.restore_change_set(
                bundle, current, restore_config=False, selected=[RESOURCE], base_digest="d" * 64,
            )

    def test_a_different_element_is_new_when_the_configuration_is_restored(self, install):
        bundle = _gather(install, _resource("a", BACKED_UP_KEY))
        # The same value at the other element: not "unchanged", which would
        # have compared it with another element's credential.
        current = _resource("b", BACKED_UP_KEY)
        row = self._row(bundle, current, restore_config=True)
        assert row["status"] == "new" and row["selectable"]
        change_set, _ = backup.restore_change_set(
            bundle, current, restore_config=True, selected=[RESOURCE], base_digest="d" * 64,
        )
        found = _preview(current, change_set)
        assert not found.blocked, (found.errors, found.blocking)
        resource = found.document["users"]["alice"]["resources"][0]
        assert resource["name"] == "a"
        assert found.inline_secrets == {"users.alice.resources.0.password": BACKED_UP_KEY}

    def test_the_same_element_restores_into_it(self, install):
        bundle = _gather(install, _resource("a", BACKED_UP_KEY))
        current = _resource("a", CURRENT_KEY)
        row = self._row(bundle, current, restore_config=False)
        assert row["status"] == "changed" and row["selectable"]
        change_set, _ = backup.restore_change_set(
            bundle, current, restore_config=False, selected=[RESOURCE], base_digest="d" * 64,
        )
        found = _preview(current, change_set)
        assert not found.blocked, (found.errors, found.blocking)
        assert found.document["users"]["alice"]["resources"][0]["name"] == "a"
        assert found.inline_secrets == {"users.alice.resources.0.password": BACKED_UP_KEY}


class TestRestoreDigests:
    def test_the_input_digest_binds_bytes_format_flag_and_selection(self):
        base = backup.restore_input_digest(b"a", fmt="kdbx", restore_config=True, selected=["x", "y"])
        assert base == backup.restore_input_digest(b"a", fmt="kdbx", restore_config=True, selected=["y", "x"])
        for other in (
            backup.restore_input_digest(b"b", fmt="kdbx", restore_config=True, selected=["x", "y"]),
            backup.restore_input_digest(b"a", fmt="toml", restore_config=True, selected=["x", "y"]),
            backup.restore_input_digest(b"a", fmt="kdbx", restore_config=False, selected=["x", "y"]),
            backup.restore_input_digest(b"a", fmt="kdbx", restore_config=True, selected=["x"]),
        ):
            assert other != base

    def test_the_preview_digest_is_canonical(self):
        assert backup.preview_digest({"a": 1, "b": [1, "é"]}) == backup.preview_digest({"b": [1, "é"], "a": 1})
        assert backup.preview_digest({"a": 1}) != backup.preview_digest({"a": 2})
