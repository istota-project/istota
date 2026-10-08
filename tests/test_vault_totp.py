"""OTP import through real KDBX files and the encrypted credential store."""
from istota.credentials import kdbx_import as credential_read

import base64
import logging

import pytest

from istota.lib.totp import parse_otpauth
from tests.support.kdbx import create_database

PASSPHRASE = "fixture-passphrase"
SEED = base64.b32encode(b"synthetic-otp-seed").decode()
OTHER_SEED = base64.b32encode(b"other-fixture-seed").decode()
URI = f"otpauth://totp/acme?secret={SEED}"


def _file(tmp_path, *, otp=None, fields=None):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password=PASSPHRASE)
    root = kp.add_group(kp.root_group, "istota")
    entry = kp.add_entry(root, "Acme", "alice", "fixture-password", url="https://acme.example")
    if otp is not None:
        entry.otp = otp
    for name, value in (fields or {}).items():
        entry.set_custom_property(name, value)
    kp.save()
    return path


def _read(path):
    return credential_read.parse_vault(path.read_bytes(), PASSPHRASE)


@pytest.mark.parametrize("otp,fields,algorithm,digits,encoder", [
    (URI, {}, "SHA1", 6, ""),
    (None, {"tOtP sEeD": SEED, "totp settings": "45;8;SHA256"}, "SHA256", 8, ""),
    (None, {"TOTP Seed": SEED, "TOTP Settings": "30;S"}, "SHA1", 5, "steam"),
    (None, {"TimeOtp-Secret-Base32": SEED, "TimeOtp-Algorithm": "HMAC-SHA-512"}, "SHA512", 6, ""),
])
def test_imports_each_format(tmp_path, otp, fields, algorithm, digits, encoder):
    read = _read(_file(tmp_path, otp=otp, fields=fields))
    assert set(read.services) == {"acme", "acme_username", "acme_url", "acme_totp"}
    params = parse_otpauth(read.services["acme_totp"])
    assert params.secret == b"synthetic-otp-seed"
    assert (params.algorithm, params.digits, params.encoder) == (algorithm, digits, encoder)
    binding = read.bindings["acme_totp"]
    assert binding["kind"] == "totp"
    assert binding["credential"] == "acme"
    assert binding["hosts"] == read.bindings["acme"]["hosts"]
    assert read.bindings["acme"].get("kind", "value") == "value"
    assert not read.skipped


@pytest.mark.parametrize("otp", [URI, None])
def test_precedence_consumes_all_losing_sources(tmp_path, otp):
    fields = {"TimeOtp-Secret-Base32": SEED if otp is None else OTHER_SEED,
              "TOTP Seed": OTHER_SEED, "TOTP Settings": "broken",
              "HmacOtp-Secret-Base32": OTHER_SEED}
    read = _read(_file(tmp_path, otp=otp, fields=fields))
    assert set(read.services) == {"acme", "acme_username", "acme_url", "acme_totp"}
    assert parse_otpauth(read.services["acme_totp"]).secret == b"synthetic-otp-seed"
    assert not read.skipped


@pytest.mark.parametrize("otp,fields,code", [
    ("malformed-sensitive-uri", {"TOTP Seed": SEED}, "uri_unparseable"),
    (URI.replace("//totp/", "//hotp/"), {"TOTP Seed": SEED}, "hotp_unsupported"),
    (None, {"TimeOtp-Period": "30", "TOTP Seed": SEED}, "secret_unparseable"),
    (None, {"HmacOtp-Secret-Base32": SEED}, "hotp_unsupported"),
    (None, {"TOTP Seed": "bad!", "TOTP Settings": "30;6"}, "secret_unparseable"),
    (None, {"TOTP Seed": ""}, "secret_unparseable"),
    (None, {"TimeOtp-Secret-Base32": ""}, "secret_unparseable"),
    (None, {"TimeOtp-Secret-Base32": SEED, "TimeOtp-Period": ""}, "period_out_of_range"),
])
def test_bad_winner_never_falls_back_or_leaks(tmp_path, caplog, otp, fields, code):
    with caplog.at_level(logging.WARNING, logger="istota.credentials.names"):
        read = _read(_file(tmp_path, otp=otp, fields=fields))
    assert set(read.services) == {"acme", "acme_username", "acme_url"}
    assert read.skipped == (("acme_totp", f"{credential_read.SKIP_UNUSABLE_OTP}: {code}"),)
    rendered = repr(read) + " ".join(r.getMessage() for r in caplog.records)
    for value in (SEED, OTHER_SEED, otp or "no-seed-here", "bad!"):
        assert value not in rendered


def test_seed_wins_over_custom_totp_field(tmp_path):
    read = _read(_file(tmp_path, otp=URI, fields={"TOTP": "custom-value"}))
    assert parse_otpauth(read.services["acme_totp"]).secret == b"synthetic-otp-seed"
    assert read.bindings["acme_totp"]["kind"] == "totp"
    assert read.skipped == (("acme_totp", credential_read.SKIP_DUPLICATE_NAME),)


def test_custom_totp_without_seed_stays_an_ordinary_value(tmp_path):
    read = _read(_file(tmp_path, fields={"TOTP": "custom-value", "TOTP Settings": "30;6"}))
    assert read.services["acme_totp"] == "custom-value"
    assert read.bindings["acme_totp"].get("kind", "value") == "value"
    assert "acme_totp_settings" not in read.services


def test_consumed_fields_still_exhaust_the_walk_budget(tmp_path, monkeypatch):
    path = _file(tmp_path, fields={f"TimeOtp-extra-{i}": "ignored" for i in range(8)})
    monkeypatch.setattr(credential_read, "VAULT_MAX_NAMES", 6)
    assert _read(path).truncated == "name"
