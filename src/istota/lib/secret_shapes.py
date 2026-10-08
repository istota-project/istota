"""Authoritative secret detection rules; the browser copy is checked for drift.

OTP validation is supplied by the caller so this leaf imports no application code.
"""
import re
import base64
from urllib.parse import parse_qs, urlsplit
from collections import Counter
from dataclasses import dataclass

BASE32_RUN = re.compile(r"(?<![A-Z0-9])[A-Z2-7]{4,}(?:[ -][A-Z2-7]{4,})*(?![A-Z0-9])")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
ENUMERATOR = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")


@dataclass(frozen=True)
class Shape:
    kind: str
    count: int
    length: int
    charset: str


class ShapeError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def parse_otp(text: str, *, totp) -> tuple[str, Shape]:
    uris = re.findall(r"otpauth://[^\s]+", text)
    runs = uris or [m.group() for m in BASE32_RUN.finditer(text.upper())
                    if len(m.group().replace(" ", "").replace("-", "")) >= 16]
    if len(runs) != 1:
        raise ShapeError("otp_ambiguous" if runs else "otp_not_found")
    try:
        params = totp.parse_user_input(runs[0])
    except totp.TotpError:
        raise ShapeError("invalid_otp") from None
    return totp.to_uri(params), Shape("otp", 1, len(params.secret), "base32")


def _charset(code):
    if code.lower() == code:
        return "[a-z0-9-]" if "-" in code else "[a-z0-9]"
    if code.upper() == code and "-" not in code:
        return "[A-Z0-9]"
    return "[A-Za-z0-9-]"


def parse_codes(text: str) -> tuple[list[str], Shape]:
    candidates = []
    for line in text.splitlines():
        line = ENUMERATOR.sub("", line)
        # Google prints each eight-digit code as two four-digit groups.
        line = re.sub(r"(?<!\d)\b(\d{4}) (\d{4})\b(?!\d)", r"\1\2", line)
        for token in line.split():
            if CODE.fullmatch(token) and any(c.isdigit() for c in token):
                candidates.append(token)
    if not candidates:
        raise ShapeError("codes_not_found")
    groups = Counter((len(c), _charset(c)) for c in candidates)
    (length, charset), count = groups.most_common(1)[0]
    if count / len(candidates) < 0.8:
        raise ShapeError("codes_inconsistent")
    codes = [c for c in candidates if (len(c), _charset(c)) == (length, charset)]
    if len(codes) > 64:
        raise ShapeError("codes_too_many")
    return codes, Shape("codes", len(codes), length, charset)


def parse_phrase(text: str) -> tuple[str, Shape]:
    text = " ".join(ENUMERATOR.sub("", line) for line in text.splitlines())
    words = re.findall(r"\b[a-z]+\b", text.lower())
    if len(words) not in (12, 15, 18, 21, 24) or any(not 3 <= len(w) <= 8 for w in words):
        raise ShapeError("phrase_word_count")
    return " ".join(words), Shape("phrase", 1, len(words), "words")


# Detection is deliberately broader than capture validation: masking must also
# hide an ambiguous or malformed block that the store would refuse.
OTP_URI = re.compile(r"otpauth://[^\s<>\"']+")
CODE_RUN = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z0-9][A-Za-z0-9-]{3,63}(?![A-Za-z0-9-])")
PHRASE_RUN = re.compile(r"\b[a-z]{3,8}(?:(?:\s+(?:[0-9]+[.)]\s*|[-*•]\s*)?)[a-z]{3,8}){11,}\b")


def mask_secret_text(text):
    text = OTP_URI.sub("[secret]", text)
    text = BASE32_RUN.sub(lambda m: "[secret]" if len(m.group().replace(" ", "").replace("-", "")) >= 16 else m.group(), text)
    text = CODE_RUN.sub(lambda m: "[secret]" if any(c.isdigit() for c in m.group()) else m.group(), text)
    return PHRASE_RUN.sub("[secret]", text)


def detect_secret_shapes(text):
    uris = OTP_URI.findall(text)
    runs = uris or [m.group() for m in BASE32_RUN.finditer(text)
                    if len(m.group().replace(" ", "").replace("-", "")) >= 16]
    if len(runs) == 1:
        seed = runs[0]
        if uris:
            seed = parse_qs(urlsplit(seed).query).get("secret", [""])[0]
        seed = seed.replace(" ", "").replace("-", "").upper().rstrip("=")
        try:
            length = len(base64.b32decode(seed + "=" * (-len(seed) % 8)))
        except (ValueError, base64.binascii.Error):
            length = 0
        if 10 <= length <= 128:
            return [{"kind_guess": "otp", "count": 1, "length": length, "charset": "base32"}]
    # An ambiguous seed is never rediscovered as a set of digit-bearing codes.
    if runs:
        return []
    for parser in (parse_codes, parse_phrase):
        try:
            _, shape = parser(text)
        except ShapeError:
            continue
        return [{"kind_guess": shape.kind, "count": shape.count,
                 "length": shape.length, "charset": shape.charset}]
    return []
