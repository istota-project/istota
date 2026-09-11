"""Stage-one contracts for the provider-neutral SMS foundation."""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import admin_config_view, db, doctor, user_profiles
from istota.config import (
    Config,
    SmsConfig,
    TelnyxSmsConfig,
    TwilioSmsConfig,
    UserConfig,
    load_config,
    sms_config_errors,
    sms_structural_config_errors,
)


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def _valid_sms_config(provider: str = "twilio") -> str:
    return f"""
[site]
hostname = "assistant.example.com"

[sms]
enabled = true
provider = "{provider}"
service_numbers = ["+15551234567"]
default_sender_number = "+15551234567"
max_segments = 6
request_timeout_seconds = 10

[sms.twilio]
account_sid = "AC00000000000000000000000000000000"
auth_token = "twilio-auth-secret"
api_key_sid = "SK00000000000000000000000000000000"
api_key_secret = "twilio-api-secret"
messaging_service_sid = "MG00000000000000000000000000000000"

[sms.telnyx]
api_key = ""
public_key = ""
messaging_profile_id = ""
"""


class TestSmsConfig:
    def test_nested_provider_fields_load_from_environment(self, tmp_path, monkeypatch):
        for name, value in {
            "ISTOTA_SMS_TWILIO_ACCOUNT_SID": "AC-env",
            "ISTOTA_SMS_TWILIO_AUTH_TOKEN": "auth-env",
            "ISTOTA_SMS_TWILIO_API_KEY_SID": "SK-env",
            "ISTOTA_SMS_TWILIO_API_KEY_SECRET": "secret-env",
            "ISTOTA_SMS_TWILIO_MESSAGING_SERVICE_SID": "MG-env",
            "ISTOTA_SMS_TELNYX_API_KEY": "telnyx-api-env",
            "ISTOTA_SMS_TELNYX_PUBLIC_KEY": "telnyx-public-env",
            "ISTOTA_SMS_TELNYX_MESSAGING_PROFILE_ID": "telnyx-profile-env",
        }.items():
            monkeypatch.setenv(name, value)
        body = _valid_sms_config().replace(
            'account_sid = "AC00000000000000000000000000000000"\n'
            'auth_token = "twilio-auth-secret"\n'
            'api_key_sid = "SK00000000000000000000000000000000"\n'
            'api_key_secret = "twilio-api-secret"\n'
            'messaging_service_sid = "MG00000000000000000000000000000000"',
            "",
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.sms.twilio.account_sid == "AC-env"
        assert cfg.sms.twilio.auth_token == "auth-env"
        assert cfg.sms.twilio.api_key_sid == "SK-env"
        assert cfg.sms.twilio.api_key_secret == "secret-env"
        assert cfg.sms.twilio.messaging_service_sid == "MG-env"
        assert cfg.sms.telnyx.api_key == "telnyx-api-env"
        assert cfg.sms.telnyx.public_key == "telnyx-public-env"
        assert cfg.sms.telnyx.messaging_profile_id == "telnyx-profile-env"

    def test_complete_twilio_config_loads(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _valid_sms_config()))

        assert cfg.sms.enabled is True
        assert cfg.sms.provider == "twilio"
        assert cfg.sms.service_numbers == ["+15551234567"]
        assert cfg.sms.twilio.api_key_secret == "twilio-api-secret"

    def test_complete_telnyx_config_loads(self, tmp_path):
        body = _valid_sms_config("telnyx").replace(
            'api_key = ""\npublic_key = ""\nmessaging_profile_id = ""',
            'api_key = "KEY-live-secret"\n'
            'public_key = "PUBLIC-KEY"\n'
            'messaging_profile_id = "40000000-0000-0000-0000-000000000000"',
        )
        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.sms.provider == "telnyx"
        assert cfg.sms.telnyx.public_key == "PUBLIC-KEY"

    @pytest.mark.parametrize(
        "replacement, expected",
        [
            ('provider = "twilio"', 'provider = "plivo"'),
            ('service_numbers = ["+15551234567"]', 'service_numbers = ["555-123-4567"]'),
            ('default_sender_number = "+15551234567"', 'default_sender_number = "+15557654321"'),
            ('max_segments = 6', 'max_segments = 11'),
            ('request_timeout_seconds = 10', 'request_timeout_seconds = 0'),
            ('hostname = "assistant.example.com"', 'hostname = ""'),
        ],
    )
    def test_enabled_config_rejects_invalid_common_or_active_values(
        self, tmp_path, replacement, expected
    ):
        old, new = replacement, expected
        with pytest.raises(ValueError, match="SMS"):
            load_config(_write_config(tmp_path, _valid_sms_config().replace(old, new)))

    def test_partial_inactive_provider_is_reported_not_rejected(self, tmp_path):
        """Half a credential block is still a mistake worth naming — but one
        doctor reports, not one that fails the load. See
        `sms_credential_errors` for why the load path cannot decide this."""
        body = _valid_sms_config().replace(
            'api_key = ""\npublic_key = ""\nmessaging_profile_id = ""',
            'api_key = "KEY-live-secret"\npublic_key = ""\nmessaging_profile_id = ""',
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert any(
            "inactive" in error and "telnyx" in error
            for error in sms_config_errors(cfg)
        )

    def test_disabled_empty_config_keeps_safe_defaults(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, "[sms]\nenabled = false\n"))

        assert cfg.sms.enabled is False
        assert cfg.sms.max_segments == 6
        assert cfg.sms.request_timeout_seconds == 10

    def test_config_can_find_and_resolve_a_users_phone(self):
        cfg = Config(users={
            "alice": UserConfig(sms_phone_number="+15551234567"),
            "bob": UserConfig(),
        })

        assert cfg.find_user_by_sms_number("+15551234567") == "alice"
        assert cfg.find_user_by_sms_number("+15557654321") is None
        assert cfg.sms_phone_number_for("alice") == "+15551234567"
        assert cfg.sms_phone_number_for("bob") is None


class TestSmsSchemaMigration:
    def test_old_profile_table_gets_phone_column_and_unique_partial_index(self, tmp_path):
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE user_profiles ("
                "user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("INSERT INTO user_profiles (user_id) VALUES ('alice')")

        db.init_db(path)

        with db.get_db(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profiles)")}
            indexes = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA index_list(user_profiles)")
            }
            value = conn.execute(
                "SELECT sms_phone_number FROM user_profiles WHERE user_id = 'alice'"
            ).fetchone()[0]
        assert "sms_phone_number" in columns
        assert indexes["idx_user_profiles_sms_phone_number"] == 1
        assert value == ""

    def test_provider_neutral_sms_tables_have_the_declared_identifiers(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            processed = {r[1] for r in conn.execute("PRAGMA table_info(processed_sms)")}
            opt_outs = {r[1] for r in conn.execute("PRAGMA table_info(sms_opt_outs)")}
            sent = {r[1] for r in conn.execute("PRAGMA table_info(sent_sms)")}

        assert {"provider", "provider_message_id", "provider_event_id", "disposition"} <= processed
        assert opt_outs == {"phone_number", "opted_out_at", "updated_at"}
        assert {
            "logical_key", "provider", "provider_message_id", "status",
            "estimated_segments", "body_sha256", "claimed_at", "attempted_at",
        } <= sent


class TestSmsPhoneAssignment:
    def test_assignment_change_clear_and_config_overlay(self, tmp_path):
        path = tmp_path / "profiles.db"
        db.init_db(path)
        user_profiles.ensure_profile(path, "alice")

        assigned = user_profiles.update_profile(
            path, "alice", sms_phone_number="+15551234567"
        )
        changed = user_profiles.update_profile(
            path, "alice", sms_phone_number="+15557654321"
        )
        cleared = user_profiles.update_profile(path, "alice", sms_phone_number="")

        assert assigned.sms_phone_number == "+15551234567"
        assert changed.sms_phone_number == "+15557654321"
        assert cleared.sms_phone_number == ""

    @pytest.mark.parametrize(
        "number",
        ["15551234567", "+05551234567", "+1555 123 4567", "+1234567", "+1234567890123456"],
    )
    def test_invalid_e164_is_rejected_without_guessing(self, tmp_path, number):
        path = tmp_path / "profiles.db"
        db.init_db(path)
        user_profiles.ensure_profile(path, "alice")

        with pytest.raises(ValueError, match="E.164"):
            user_profiles.update_profile(path, "alice", sms_phone_number=number)

    def test_non_empty_phone_number_is_unique_with_a_useful_error(self, tmp_path):
        path = tmp_path / "profiles.db"
        db.init_db(path)
        user_profiles.ensure_profile(path, "alice")
        user_profiles.ensure_profile(path, "bob")
        user_profiles.update_profile(path, "alice", sms_phone_number="+15551234567")

        with pytest.raises(ValueError, match="already assigned") as excinfo:
            user_profiles.update_profile(path, "bob", sms_phone_number="+15551234567")

        assert "UNIQUE constraint" not in str(excinfo.value)
        assert user_profiles.get_profile(path, "bob").sms_phone_number == ""

    def test_human_rendering_masks_but_operator_json_may_return_the_number(self):
        assert user_profiles.mask_sms_phone_number("+15551234567") == "+*******4567"

    def test_user_ensure_assigns_and_clears_without_printing_the_number(
        self, tmp_path, capsys
    ):
        from istota.cli import cmd_user_ensure

        path = tmp_path / "profiles.db"
        db.init_db(path)
        config_path = _write_config(tmp_path, f'db_path = "{path}"\n')
        defaults = {
            "config": str(config_path),
            "name": "alice",
            "display_name": None,
            "tz": None,
            "email": None,
            "trusted_sender": None,
            "quiet_sender": None,
            "log_channel": None,
            "alerts_channel": None,
            "max_foreground_workers": None,
            "max_background_workers": None,
            "disabled_skill": None,
            "disabled_module": None,
            "default_destination": None,
            "default_room": None,
            "route": None,
            "email_reply_routing": None,
            "outbound_approval": None,
            "external_turn_display": None,
            "default_briefings": None,
            "briefing_email_html": None,
            "timezone_follow_location": None,
            "clear_sms_number": False,
        }

        cmd_user_ensure(SimpleNamespace(
            **defaults, sms_number="+15551234567",
        ))
        output = capsys.readouterr().out
        assert "+15551234567" not in output
        assert "+*******4567" in output
        assert user_profiles.get_profile(path, "alice").sms_phone_number == "+15551234567"

        defaults["clear_sms_number"] = True
        cmd_user_ensure(SimpleNamespace(**defaults, sms_number=None))
        assert user_profiles.get_profile(path, "alice").sms_phone_number == ""

    def test_assignment_flags_are_mutually_exclusive(self, monkeypatch):
        from istota import cli

        monkeypatch.setattr(sys, "argv", [
            "istota", "user", "ensure", "--name", "alice",
            "--sms-number", "+15551234567", "--clear-sms-number",
        ])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2


class TestSmsSecretRedaction:
    @pytest.mark.parametrize(
        "dotted",
        [
            "sms.twilio.auth_token",
            "sms.twilio.api_key_sid",
            "sms.twilio.api_key_secret",
            "sms.telnyx.api_key",
        ],
    )
    def test_every_provider_secret_is_redacted(self, dotted):
        cfg = Config()
        target = cfg
        *path, leaf = dotted.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, leaf, "SMS-SENTINEL-SECRET")

        payload = admin_config_view.build_config_view(cfg)
        fields = {
            field["key"]: field
            for section in payload["sections"]
            for field in section["fields"]
        }
        assert "SMS-SENTINEL-SECRET" not in str(payload)
        assert fields[dotted]["secret"] is True


class TestSmsProviderContract:
    def test_provider_records_are_immutable_and_adapter_is_callable(self):
        from istota.transport.sms.providers._types import (
            InboundSmsEvent,
            SmsProviderAdapter,
            SmsSendFailure,
        )

        event = InboundSmsEvent(
            provider="twilio",
            provider_event_id=None,
            provider_message_id="message-1",
            from_number="+15551234567",
            to_number="+15557654321",
            text="hello",
            media_count=0,
            opt_out_action=None,
        )
        adapter = SmsProviderAdapter(
            name="twilio",
            parse_webhook=lambda request: request,
            send=lambda request: SmsSendFailure(
                definite=False,
                error_code=None,
                opted_out=False,
                safe_reason="delivery state unknown",
            ),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            event.text = "changed"
        assert adapter.name == "twilio"

    def test_registry_distinguishes_active_and_callback_only_adapters(self):
        from istota.transport.sms.providers._types import SmsProviderAdapter
        from istota.transport.sms.providers.registry import SmsProviderRegistry

        twilio = SmsProviderAdapter("twilio", lambda request: request, lambda request: request)
        telnyx = SmsProviderAdapter("telnyx", lambda request: request, lambda request: request)
        registry = SmsProviderRegistry(
            active_name="telnyx",
            adapters={"twilio": twilio, "telnyx": telnyx},
        )

        assert registry.active() is telnyx
        assert registry.get("twilio") is twilio
        assert registry.callback_only_names() == ("twilio",)

    def test_registry_builds_only_complete_configured_adapters(self):
        from istota.transport.sms.providers._types import SmsProviderAdapter
        from istota.transport.sms.providers.registry import make_provider_registry

        cfg = Config()
        cfg.sms.enabled = True
        cfg.sms.provider = "twilio"
        cfg.sms.twilio.account_sid = "account"
        cfg.sms.twilio.auth_token = "auth"
        cfg.sms.twilio.api_key_sid = "key"
        cfg.sms.twilio.api_key_secret = "secret"
        cfg.sms.twilio.messaging_service_sid = "service"
        built = SmsProviderAdapter("twilio", lambda request: request, lambda request: request)

        registry = make_provider_registry(
            cfg, builders={"twilio": lambda config: built},
        )

        assert registry.active() is built
        assert registry.names() == ("twilio",)

    def test_disabled_sms_keeps_complete_adapters_for_late_callbacks(self):
        from istota.transport.sms.providers._types import SmsProviderAdapter
        from istota.transport.sms.providers.registry import make_provider_registry

        cfg = Config()
        cfg.sms.twilio.account_sid = "account"
        cfg.sms.twilio.auth_token = "auth"
        cfg.sms.twilio.api_key_sid = "key"
        cfg.sms.twilio.api_key_secret = "secret"
        cfg.sms.twilio.messaging_service_sid = "service"
        built = SmsProviderAdapter("twilio", lambda request: request, lambda request: request)

        registry = make_provider_registry(
            cfg, builders={"twilio": lambda config: built},
        )

        assert registry.active() is None
        assert registry.get("twilio") is built
        assert registry.callback_only_names() == ("twilio",)


class TestSmsDoctorReadiness:
    def _results(self, cfg):
        return doctor.run_checks(cfg, only=("sms.",), probe=False)

    def test_disabled_sms_is_reported_as_skipped(self):
        results = self._results(Config())
        assert results
        assert all(result.status == doctor.SKIP for result in results)

    def test_active_and_callback_only_providers_are_named_without_claiming_delivery(self):
        cfg = Config()
        cfg.site.hostname = "assistant.example.com"
        cfg.sms.enabled = True
        cfg.sms.provider = "twilio"
        cfg.sms.service_numbers = ["+15551234567"]
        cfg.sms.default_sender_number = "+15551234567"
        cfg.sms.twilio.account_sid = "AC-example"
        cfg.sms.twilio.auth_token = "auth-secret"
        cfg.sms.twilio.api_key_sid = "SK-example"
        cfg.sms.twilio.api_key_secret = "api-secret"
        cfg.sms.twilio.messaging_service_sid = "MG-example"
        cfg.sms.telnyx.api_key = "telnyx-secret"
        cfg.sms.telnyx.public_key = "public-key"
        cfg.sms.telnyx.messaging_profile_id = "profile-id"

        results = {result.name: result for result in self._results(cfg)}

        assert results["sms.common"].status == doctor.OK
        assert "active" in results["sms.twilio"].detail
        assert "callback-only" in results["sms.telnyx"].detail
        assert "carrier" not in " ".join(result.detail.lower() for result in results.values())

    def test_partial_provider_block_fails_without_rendering_a_secret(self):
        cfg = Config()
        cfg.sms.telnyx.api_key = "SMS-SENTINEL-SECRET"

        results = {result.name: result for result in self._results(cfg)}

        assert results["sms.telnyx"].status == doctor.FAIL
        assert "SMS-SENTINEL-SECRET" not in str(results["sms.telnyx"])

    def test_disabled_sms_reports_complete_provider_as_callback_only(self):
        cfg = Config()
        cfg.sms.telnyx.api_key = "telnyx-secret"
        cfg.sms.telnyx.public_key = "public-key"
        cfg.sms.telnyx.messaging_profile_id = "profile-id"

        results = {result.name: result for result in self._results(cfg)}

        assert results["sms.common"].status == doctor.SKIP
        assert results["sms.telnyx"].status == doctor.OK
        assert "callback-only" in results["sms.telnyx"].detail


def test_a_users_toml_block_binds_an_sms_number(tmp_path):
    """`[users.X] sms_phone_number` is the Docker entrypoint's path in.

    It was the one writer of this field that read nothing, so an operator on
    the `[users.X]` shape set a number and got no binding — and the failure is
    silent, because an unbound user simply never receives or sends SMS.
    """
    from istota.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[users.alice]\n'
        'display_name = "Alice"\n'
        'sms_phone_number = "+15551234567"\n'
    )
    config = load_config(path)

    assert config.users["alice"].sms_phone_number == "+15551234567"
    assert config.find_user_by_sms_number("+15551234567") == "alice"


def test_a_users_toml_block_refuses_a_number_that_is_not_e164(tmp_path):
    from istota.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[users.alice]\nsms_phone_number = "555-1234"\n')

    with pytest.raises(ValueError, match="E.164"):
        load_config(path)


def test_an_empty_number_matches_no_user(tmp_path):
    """`sms_phone_number` defaults to `''`, so a bare equality test on an empty
    argument returns the first *unbound* user — and this answer decides which
    user an inbound message acts as."""
    from istota.config import Config, UserConfig

    config = Config(users={
        "alice": UserConfig(sms_phone_number=""),
        "bob": UserConfig(sms_phone_number="+15551234567"),
    })

    assert config.find_user_by_sms_number("") is None
    assert config.find_user_by_sms_number("not-a-number") is None
    assert config.find_user_by_sms_number("+15551234567") == "bob"


def test_every_sms_provider_secret_is_redacted_and_no_number_is_rendered():
    """Named coverage for the SMS block, which the generic guards cover only
    by name pattern — so a field whose name stopped matching would be caught
    there, and a *phone number*, which matches no credential pattern at all,
    would be caught by nothing.

    Phone values are operational data and must not appear in operator output.
    """
    config = Config(
        site=SimpleNamespace(hostname="assistant.example.test"),
        sms=SmsConfig(
            enabled=True,
            provider="twilio",
            service_numbers=["+15550001111"],
            default_sender_number="+15550001111",
            twilio=TwilioSmsConfig(
                account_sid="AC-account",
                auth_token="fabricated-auth-token",
                api_key_sid="SK-key",
                api_key_secret="fabricated-api-secret",
                messaging_service_sid="MG-service",
            ),
            telnyx=TelnyxSmsConfig(
                api_key="fabricated-telnyx-key",
                public_key="fabricated-public-key",
                messaging_profile_id="MP-profile",
            ),
        ),
        users={"alice": UserConfig(sms_phone_number="+15559998888")},
    )

    rendered = repr(admin_config_view.build_config_view(config))

    for secret in (
        "fabricated-auth-token",
        "fabricated-api-secret",
        "fabricated-telnyx-key",
    ):
        assert secret not in rendered, f"{secret!r} reached the admin config view"
    assert "+15559998888" not in rendered, "a user's phone number was rendered"


class TestCredentialPresenceDoesNotBlockConfigLoad:
    """Credential *presence* is not a load-time error.

    Under `istota_use_environment_file` the Ansible role deliberately renders
    `[sms.telnyx]` empty and puts the credentials in `/etc/<ns>/secrets.env`,
    which systemd hands to the units and which a plain `command:`/`script:`
    task cannot read. Raising here meant the role's own deploy-time tasks —
    the ISSUE-058 config validator among them, which touches no SMS — failed
    on a config the daemon they were validating for would have loaded fine.
    Every other credential in `_env_secret_overrides` already behaves this
    way: absent at load, reported by doctor, fatal only at use.
    """

    def test_active_provider_with_no_credentials_anywhere_still_loads(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _valid_sms_config("telnyx")))

        assert cfg.sms.enabled is True
        assert cfg.sms.provider == "telnyx"
        assert cfg.sms.telnyx.api_key == ""

    def test_partial_inactive_provider_still_loads(self, tmp_path):
        body = _valid_sms_config().replace(
            'api_key = ""\npublic_key = ""\nmessaging_profile_id = ""',
            'api_key = "KEY-live-secret"\npublic_key = ""\nmessaging_profile_id = ""',
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.sms.telnyx.public_key == ""

    def test_the_credential_gap_is_still_reported_rather_than_silently_dropped(self):
        cfg = Config()
        cfg.site.hostname = "assistant.example.com"
        cfg.sms.enabled = True
        cfg.sms.provider = "telnyx"
        cfg.sms.service_numbers = ["+15551234567"]
        cfg.sms.default_sender_number = "+15551234567"

        errors = sms_config_errors(cfg)

        assert any("incomplete" in error for error in errors)
        assert sms_structural_config_errors(cfg) == []

    @pytest.mark.parametrize(
        "old, new",
        [
            ('provider = "twilio"', 'provider = "plivo"'),
            ('service_numbers = ["+15551234567"]', 'service_numbers = ["555-123-4567"]'),
            (
                'default_sender_number = "+15551234567"',
                'default_sender_number = "+15557654321"',
            ),
            ('max_segments = 6', 'max_segments = 11'),
            ('request_timeout_seconds = 10', 'request_timeout_seconds = 0'),
            ('hostname = "assistant.example.com"', 'hostname = ""'),
        ],
    )
    def test_structural_errors_still_fail_the_load(self, tmp_path, old, new):
        with pytest.raises(ValueError, match="SMS"):
            load_config(_write_config(tmp_path, _valid_sms_config().replace(old, new)))
