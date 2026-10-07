"""TOTP formats meet at one canonical URI, without secrets in diagnostics."""

import base64
import hashlib
import random
import traceback
from dataclasses import FrozenInstanceError

import pytest

from istota.lib.totp import (
    TOTP_ERRORS, TotpError, TotpParams, code_at, parse_keepass_timeotp,
    parse_keepassxc_legacy, parse_otpauth, parse_user_input, to_uri, window,
)


SECRET = b"12345678901234567890"  # RFC 6238 public test vector.
SEED = base64.b32encode(SECRET).decode().rstrip("=")
URI = f"otpauth://totp/example?secret={SEED}"


@pytest.mark.parametrize("timestamp,expected", [
    (59, ("94287082", "46119246", "90693936")),
    (1111111109, ("07081804", "68084774", "25091201")),
    (1111111111, ("14050471", "67062674", "99943326")),
    (1234567890, ("89005924", "91819424", "93441116")),
    (2000000000, ("69279037", "90698825", "38618901")),
    (20000000000, ("65353130", "77737706", "47863826")),
])
def test_rfc_6238_appendix_b(timestamp, expected):
    for algorithm, length, code in zip(("SHA1", "SHA256", "SHA512"), (20, 32, 64), expected):
        secret = (SECRET * 4)[:length]
        params = TotpParams(secret, 8, 30, algorithm, "")
        assert code_at(params, timestamp) == code


def test_seeded_pyotp_oracle():
    pyotp = pytest.importorskip("pyotp")
    rng = random.Random(6238)
    for _ in range(100):
        secret = rng.randbytes(rng.randint(10, 128))
        digits = rng.randint(6, 10)
        period = rng.randint(15, 300)
        algorithm = rng.choice(("SHA1", "SHA256", "SHA512"))
        timestamp = rng.randint(0, 2_000_000_000) + rng.random()
        params = TotpParams(secret, digits, period, algorithm, "")
        oracle = pyotp.TOTP(base64.b32encode(secret).decode(), digits=digits,
                            interval=period, digest=getattr(hashlib, algorithm.lower()))
        assert code_at(params, timestamp) == oracle.at(timestamp)


def test_steam_vector_and_oracle():
    params = parse_keepassxc_legacy(SEED, "30;S")
    assert params.digits == 5
    assert params.encoder == "steam"
    assert code_at(params, 59) == "PV9M4"
    steam = pytest.importorskip("pyotp.contrib.steam")
    assert code_at(params, 1234567890) == steam.Steam(SEED).at(1234567890)


@pytest.mark.parametrize("size", [10, 11, 128])
def test_base32_normalization_and_length_boundaries(size):
    secret = bytes(range(size))
    encoded = base64.b32encode(secret).decode()
    decorated = " -".join(encoded.rstrip("=").lower())
    assert parse_user_input(decorated).secret == secret
    assert parse_user_input(encoded).secret == secret


def test_otpauth_defaults_overrides_and_unknown_keys():
    assert parse_otpauth(URI) == TotpParams(SECRET, 6, 30, "SHA1", "")
    assert parse_otpauth(URI + "&algorithm=sha256&digits=10&period=300&issuer=example&other=ignored") == (
        TotpParams(SECRET, 10, 300, "SHA256", "")
    )
    assert parse_otpauth(URI + "&encoder=steam&digits=6").digits == 5
    assert parse_user_input(" \n" + URI + "\n ") == parse_otpauth(URI)


@pytest.mark.parametrize("settings,digits,period,algorithm,encoder", [
    (None, 6, 30, "SHA1", ""), ("", 6, 30, "SHA1", ""),
    ("15;6", 6, 15, "SHA1", ""), ("300;10;sha512", 10, 300, "SHA512", ""),
    ("30;S", 5, 30, "SHA1", "" + "steam"),
])
def test_legacy_forms(settings, digits, period, algorithm, encoder):
    assert parse_keepassxc_legacy(SEED, settings) == TotpParams(SECRET, digits, period, algorithm, encoder)


@pytest.mark.parametrize("field,value", [
    ("TimeOtp-Secret-Base32", SEED), ("TimeOtp-Secret-Hex", SECRET.hex()),
    ("TimeOtp-Secret-Base64", base64.b64encode(SECRET).decode()),
    ("TimeOtp-Secret", SECRET.decode()),
])
@pytest.mark.parametrize("algorithm", ["1", "256", "512"])
def test_keepass_formats(field, value, algorithm):
    fields = {field: value, "TimeOtp-Length": "8", "TimeOtp-Period": "60",
              "TimeOtp-Algorithm": "HMAC-SHA-" + algorithm}
    assert parse_keepass_timeotp(fields) == TotpParams(SECRET, 8, 60, "SHA" + algorithm, "")


def test_keepass_precedence_and_case_insensitive_names():
    fields = {"TimeOtp-Secret-Base32": SEED, "TimeOtp-Secret-Hex": b"h" * 20,
              "TimeOtp-Secret-Base64": "invalid!", "TimeOtp-Secret": "plain seed value"}
    assert parse_keepass_timeotp({k.lower(): v for k, v in fields.items()}).secret == SECRET
    del fields["TimeOtp-Secret-Base32"]
    fields["TimeOtp-Secret-Hex"] = SECRET.hex()
    assert parse_keepass_timeotp(fields).secret == SECRET
    del fields["TimeOtp-Secret-Hex"]
    fields["TimeOtp-Secret-Base64"] = base64.b64encode(SECRET).decode()
    assert parse_keepass_timeotp(fields).secret == SECRET
    fields["TimeOtp-Secret-Base32"] = "!invalid"
    with pytest.raises(TotpError) as caught:
        parse_keepass_timeotp(fields)
    assert caught.value.code == "secret_unparseable"


@pytest.mark.parametrize("parser,args,error", [
    (parse_user_input, (base64.b32encode(b"a" * 9).decode(),), "secret_length"),
    (parse_user_input, (base64.b32encode(b"a" * 129).decode(),), "secret_length"),
    (parse_user_input, ("invalid!seed",), "secret_unparseable"),
    (parse_otpauth, ("https://totp/example?secret=" + SEED,), "uri_unparseable"),
    (parse_otpauth, ("otpauth://[invalid",), "uri_unparseable"),
    (parse_otpauth, ("otpauth://hotp/example?secret=" + SEED,), "hotp_unsupported"),
    (parse_otpauth, ("otpauth://totp/example",), "secret_unparseable"),
    (parse_otpauth, (URI + "&secret=" + SEED,), "uri_unparseable"),
    (parse_otpauth, (URI + "&encoder=unknown",), "settings_unparseable"),
    (parse_otpauth, (URI + "&algorithm=md5",), "algorithm_unsupported"),
    (parse_otpauth, (URI + "&digits=5",), "digits_out_of_range"),
    (parse_otpauth, (URI + "&digits=11",), "digits_out_of_range"),
    (parse_otpauth, (URI + "&digits=secret-text",), "digits_out_of_range"),
    (parse_otpauth, (URI + "&period=14",), "period_out_of_range"),
    (parse_otpauth, (URI + "&period=301",), "period_out_of_range"),
    (parse_otpauth, (URI + "&period=secret-text",), "period_out_of_range"),
    (parse_keepassxc_legacy, (SEED, "30"), "settings_unparseable"),
    (parse_keepassxc_legacy, (SEED, "30;6;SHA1;extra"), "settings_unparseable"),
    (parse_keepassxc_legacy, (SEED, "30;S;SHA1"), "settings_unparseable"),
    (parse_keepassxc_legacy, (SEED, "P;D"), "settings_unparseable"),
    (parse_keepass_timeotp, ({},), "secret_unparseable"),
    (parse_keepass_timeotp, ({"TimeOtp-Secret-Hex": "not hex"},), "secret_unparseable"),
    (parse_keepass_timeotp, ({"TimeOtp-Secret-Base64": "not base64!"},), "secret_unparseable"),
    (parse_keepass_timeotp, ({"TimeOtp-Secret": "\ud800"},), "secret_unparseable"),
    (parse_keepass_timeotp, ({"TimeOtp-Secret": SECRET.decode(), "TimeOtp-Algorithm": "MD5"},), "algorithm_unsupported"),
])
def test_errors_are_fixed_and_do_not_echo_input(parser, args, error, caplog):
    with pytest.raises(TotpError) as caught:
        parser(*args)
    exc = caught.value
    assert exc.code == error
    assert exc.code in TOTP_ERRORS
    assert str(exc) == str(TotpError(error))
    diagnostic = str(exc) + repr(exc) + "".join(traceback.format_exception(exc)) + caplog.text
    for sensitive in (SEED, SECRET.decode(), URI, "secret-text", "invalid!seed", "not hex", "not base64!"):
        assert sensitive not in diagnostic


@pytest.mark.parametrize("algorithm", ["SHA1", "SHA256", "SHA512"])
@pytest.mark.parametrize("encoder", ["", "steam"])
def test_canonical_uri_round_trip(algorithm, encoder):
    params = TotpParams(SECRET, 5 if encoder else 8, 60, algorithm, encoder)
    uri = to_uri(params, label="example:alice /?&#")
    assert uri.startswith("otpauth://totp/example%3Aalice%20%2F%3F%26%23?")
    assert parse_otpauth(uri) == params
    assert to_uri(parse_otpauth(uri), label="example:alice /?&#") == uri
    assert code_at(parse_user_input(uri), 59) == code_at(params, 59)


@pytest.mark.parametrize("timestamp,expected", [(0, (0, 30)), (29.999, (0, 30)), (30, (30, 60)), (60, (60, 90))])
def test_window_boundaries(timestamp, expected):
    assert window(parse_otpauth(URI), timestamp) == expected


def test_params_are_frozen_and_repr_omits_secret():
    params = parse_otpauth(URI)
    assert SECRET.decode() not in repr(params)
    assert SEED not in repr(params)
    assert URI not in repr(params)
    with pytest.raises(FrozenInstanceError):
        params.period = 60
    assert len(TOTP_ERRORS) == 8
    for error in TOTP_ERRORS:
        assert SEED not in repr(TotpError(error))
