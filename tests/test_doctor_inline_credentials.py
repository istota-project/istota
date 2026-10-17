"""`security.inline_credentials`: installation credentials left in config.toml."""

from __future__ import annotations

import dataclasses
import json

from istota import doctor
from istota.config import Config


def _config(tmp_path, text: str | None) -> Config:
    path = tmp_path / "config.toml"
    if text is not None:
        path.write_text(text)
    return Config(config_path=path)


def _serialized(result) -> str:
    return json.dumps(dataclasses.asdict(result))


def test_it_is_registered():
    names = [name for name, _check in doctor.CHECKS]
    assert "security.inline_credentials" in names


def test_ok_when_no_credential_is_inline(tmp_path):
    config = _config(tmp_path, '[email]\nimap_host = "imap.example.org"\nimap_password = ""\n')
    result = doctor.check_inline_credentials(config, probe=False)
    assert result.name == "security.inline_credentials"
    assert result.status == doctor.OK
    assert result.scope == doctor.DEPLOYMENT


def test_warns_naming_each_inline_path_and_its_file(tmp_path):
    config = _config(tmp_path, (
        '[sms.twilio]\nauth_token = "TWILIO-TOKEN-SENTINEL"\n\n'
        '[brain.native.extra_headers]\nX-Key = "HEADER-VALUE-SENTINEL"\n\n'
        '[web.map]\napi_key = "MAP-KEY-SENTINEL"\n'
    ))
    result = doctor.check_inline_credentials(config, probe=False)
    assert result.status == doctor.WARN
    assert "sms.twilio.auth_token (istota_sms_twilio_auth_token)" in result.detail
    assert "brain.native.extra_headers (istota_brain_native_extra_headers)" in result.detail
    assert "web.map" not in result.detail
    assert "Admin > Secrets" in result.remedy
    assert "Move to secret file" in result.remedy
    serialized = _serialized(result)
    for value in ("TWILIO-TOKEN-SENTINEL", "HEADER-VALUE-SENTINEL", "X-Key", "MAP-KEY-SENTINEL"):
        assert value not in serialized


def test_a_servarr_key_under_its_app_table_is_found(tmp_path):
    config = _config(tmp_path, '[servarr.apps.sonarr]\nurl = "http://sonarr:8989"\napi_key = "SONARR-SENTINEL"\n')
    result = doctor.check_inline_credentials(config, probe=False)
    assert result.status == doctor.WARN
    assert "servarr.apps.sonarr.api_key (istota_servarr_sonarr_api_key)" in result.detail
    assert "SONARR-SENTINEL" not in _serialized(result)


def test_skips_without_a_config_file(tmp_path):
    result = doctor.check_inline_credentials(Config(), probe=False)
    assert result.status == doctor.SKIP


def test_an_unreadable_file_warns_with_the_error_class_only(tmp_path):
    config = _config(tmp_path, 'password = "BROKEN-SENTINEL\n')
    result = doctor.check_inline_credentials(config, probe=False)
    assert result.status == doctor.WARN
    assert "TOMLDecodeError" in result.detail
    assert "BROKEN-SENTINEL" not in _serialized(result)
    assert result.remedy


def test_a_missing_file_warns(tmp_path):
    result = doctor.check_inline_credentials(_config(tmp_path, None), probe=False)
    assert result.status == doctor.WARN
    assert "FileNotFoundError" in result.detail
