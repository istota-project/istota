"""Parse vault TOTP formats and generate codes without optional dependencies.

Only ``to_uri`` deliberately returns the seed. Diagnostics contain fixed
messages and the parameter repr omits the decoded secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlencode, urlsplit


_ERROR_MESSAGES = {
    "secret_length": "The TOTP secret must contain 10 to 128 bytes.",
    "secret_unparseable": "The TOTP secret could not be decoded.",
    "uri_unparseable": "The TOTP URI could not be parsed.",
    "hotp_unsupported": "Counter-based OTP is not supported.",
    "settings_unparseable": "The TOTP settings could not be parsed.",
    "algorithm_unsupported": "The TOTP algorithm is not supported.",
    "digits_out_of_range": "The TOTP digit count is outside the supported range.",
    "period_out_of_range": "The TOTP period must be 15 to 300 seconds.",
}
TOTP_ERRORS = frozenset(_ERROR_MESSAGES)
_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
_STEAM_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"


class TotpError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in TOTP_ERRORS else "settings_unparseable"
        super().__init__(_ERROR_MESSAGES[self.code])


@dataclass(frozen=True)
class TotpParams:
    secret: bytes = field(repr=False)
    digits: int
    period: int
    algorithm: str
    encoder: str

    def __post_init__(self):
        if not 10 <= len(self.secret) <= 128:
            raise TotpError("secret_length")
        if self.algorithm not in _ALGORITHMS:
            raise TotpError("algorithm_unsupported")
        if self.encoder not in ("", "steam"):
            raise TotpError("settings_unparseable")
        if type(self.digits) is not int or not (
            self.digits == 5 if self.encoder == "steam" else 6 <= self.digits <= 10
        ):
            raise TotpError("digits_out_of_range")
        if type(self.period) is not int or not 15 <= self.period <= 300:
            raise TotpError("period_out_of_range")


def _base32(text: str) -> bytes:
    normalized = text.replace(" ", "").replace("-", "").upper()
    if not normalized:
        raise TotpError("secret_unparseable")
    normalized += "=" * (-len(normalized) % 8)
    try:
        return base64.b32decode(normalized)
    except (ValueError, binascii.Error):
        raise TotpError("secret_unparseable") from None


def _integer(text: str, error: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise TotpError(error) from None


def parse_otpauth(uri: str) -> TotpParams:
    try:
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise TotpError("uri_unparseable") from None
    if parsed.scheme != "otpauth":
        raise TotpError("uri_unparseable")
    if parsed.netloc == "hotp":
        raise TotpError("hotp_unsupported")
    if parsed.netloc != "totp":
        raise TotpError("uri_unparseable")
    for name in ("secret", "algorithm", "digits", "period", "encoder"):
        if len(query.get(name, [])) > 1:
            raise TotpError("uri_unparseable")
    secret = _base32(query.get("secret", [""])[0])
    encoder = query.get("encoder", [""])[0]
    digits = 5 if encoder == "steam" else _integer(query.get("digits", ["6"])[0], "digits_out_of_range")
    period = _integer(query.get("period", ["30"])[0], "period_out_of_range")
    algorithm = query.get("algorithm", ["SHA1"])[0].upper()
    return TotpParams(secret, digits, period, algorithm, encoder)


def parse_keepassxc_legacy(seed: str, settings: str | None) -> TotpParams:
    parts = (settings or "30;6").split(";")
    if len(parts) not in (2, 3) or (parts[1] == "S" and len(parts) != 2):
        raise TotpError("settings_unparseable")
    period = _integer(parts[0], "settings_unparseable")
    encoder = "steam" if parts[1] == "S" else ""
    digits = 5 if encoder else _integer(parts[1], "settings_unparseable")
    algorithm = parts[2].upper() if len(parts) == 3 else "SHA1"
    return TotpParams(_base32(seed), digits, period, algorithm, encoder)


def parse_keepass_timeotp(fields: Mapping[str, str]) -> TotpParams:
    fields = {name.casefold(): value for name, value in fields.items()}
    try:
        if "timeotp-secret-base32" in fields:
            secret = _base32(fields["timeotp-secret-base32"])
        elif "timeotp-secret-hex" in fields:
            secret = bytes.fromhex(fields["timeotp-secret-hex"])
        elif "timeotp-secret-base64" in fields:
            secret = base64.b64decode(fields["timeotp-secret-base64"], validate=True)
        elif "timeotp-secret" in fields:
            secret = fields["timeotp-secret"].encode("utf-8")
        else:
            raise TotpError("secret_unparseable")
    except (ValueError, binascii.Error):
        raise TotpError("secret_unparseable") from None
    algorithm = {
        "HMAC-SHA-1": "SHA1", "HMAC-SHA-256": "SHA256", "HMAC-SHA-512": "SHA512",
    }.get(fields.get("timeotp-algorithm", "HMAC-SHA-1").upper())
    if algorithm is None:
        raise TotpError("algorithm_unsupported")
    digits = _integer(fields.get("timeotp-length", "6"), "digits_out_of_range")
    period = _integer(fields.get("timeotp-period", "30"), "period_out_of_range")
    return TotpParams(secret, digits, period, algorithm, "")


def parse_user_input(text: str) -> TotpParams:
    text = text.strip()
    if "://" in text:
        return parse_otpauth(text)
    return parse_keepassxc_legacy(text, None)


def to_uri(params: TotpParams, *, label: str = "istota") -> str:
    query = {
        "secret": base64.b32encode(params.secret).decode("ascii").rstrip("="),
        "algorithm": params.algorithm,
        "digits": str(params.digits),
        "period": str(params.period),
    }
    if params.encoder:
        query["encoder"] = params.encoder
    return "otpauth://totp/" + quote(label, safe="") + "?" + urlencode(query)


def window(params: TotpParams, unix_time: float) -> tuple[int, int]:
    start = math.floor(unix_time / params.period) * params.period
    return start, start + params.period


def code_at(params: TotpParams, unix_time: float) -> str:
    start, _ = window(params, unix_time)
    counter = struct.pack(">Q", start // params.period)
    digest = hmac.new(params.secret, counter, _ALGORITHMS[params.algorithm]).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    if params.encoder == "steam":
        code = ""
        for _ in range(5):
            code += _STEAM_ALPHABET[value % len(_STEAM_ALPHABET)]
            value //= len(_STEAM_ALPHABET)
        return code
    return str(value % (10 ** params.digits)).zfill(params.digits)
